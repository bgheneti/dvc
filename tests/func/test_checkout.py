import collections
import filecmp
import logging
import os
import shutil

import pytest
from mock import patch

from dvc.dvcfile import DVC_FILE_SUFFIX, PIPELINE_FILE, Dvcfile
from dvc.exceptions import (
    CheckoutError,
    CheckoutErrorSuggestGit,
    ConfirmRemoveError,
    DvcException,
    NoOutputOrStageError,
)
from dvc.main import main
from dvc.remote.base import CloudCache, Remote
from dvc.remote.local import LocalRemoteTree
from dvc.remote.s3 import S3RemoteTree
from dvc.repo import Repo as DvcRepo
from dvc.stage import Stage
from dvc.stage.exceptions import StageFileDoesNotExistError
from dvc.system import System
from dvc.utils import relpath
from dvc.utils.fs import walk_files
from dvc.utils.yaml import dump_yaml, load_yaml
from tests.basic_env import TestDvc, TestDvcGit
from tests.func.test_repro import TestRepro
from tests.remotes import S3

logger = logging.getLogger("dvc")


class TestCheckout(TestRepro):
    def setUp(self):
        super().setUp()

        stages = self.dvc.add(self.DATA_DIR)
        self.assertEqual(len(stages), 1)
        self.data_dir_stage = stages[0]
        self.assertTrue(self.data_dir_stage is not None)

        self.orig = "orig"
        shutil.copy(self.FOO, self.orig)
        os.unlink(self.FOO)

        self.orig_dir = "orig_dir"
        shutil.copytree(self.DATA_DIR, self.orig_dir)
        shutil.rmtree(self.DATA_DIR)

    def test(self):
        self.dvc.checkout(force=True)
        self._test_checkout()

    def _test_checkout(self):
        self.assertTrue(os.path.isfile(self.FOO))
        self.assertTrue(filecmp.cmp(self.FOO, self.orig, shallow=False))


class TestCheckoutSingleStage(TestCheckout):
    def test(self):
        ret = main(["checkout", "--force", self.foo_stage.path])
        self.assertEqual(ret, 0)

        ret = main(["checkout", "--force", self.data_dir_stage.path])
        self.assertEqual(ret, 0)

        self._test_checkout()


class TestCheckoutCorruptedCacheFile(TestRepro):
    def test(self):
        cache = self.foo_stage.outs[0].cache_path

        os.chmod(cache, 0o644)
        with open(cache, "a") as fd:
            fd.write("1")

        with pytest.raises(CheckoutError):
            self.dvc.checkout(force=True)

        self.assertFalse(os.path.isfile(self.FOO))
        self.assertFalse(os.path.isfile(cache))


class TestCheckoutCorruptedCacheDir(TestDvc):
    def test(self):
        # NOTE: using 'copy' so that cache and link don't have same inode
        ret = main(["config", "cache.type", "copy"])
        self.assertEqual(ret, 0)

        self.dvc = DvcRepo(".")
        stages = self.dvc.add(self.DATA_DIR)
        self.assertEqual(len(stages), 1)
        self.assertEqual(len(stages[0].outs), 1)
        out = stages[0].outs[0]

        # NOTE: modifying cache file for one of the files inside the directory
        # to check if dvc will detect that the cache is corrupted.
        entry = self.dvc.cache.local.load_dir_cache(out.checksum)[0]
        entry_hash = entry[self.dvc.cache.local.tree.PARAM_CHECKSUM]
        cache = os.fspath(self.dvc.cache.local.hash_to_path_info(entry_hash))

        os.chmod(cache, 0o644)
        with open(cache, "w+") as fobj:
            fobj.write("1")

        with pytest.raises(CheckoutError):
            self.dvc.checkout(force=True)

        self.assertFalse(os.path.exists(cache))


class TestCmdCheckout(TestCheckout):
    def test(self):
        ret = main(["checkout", "--force"])
        self.assertEqual(ret, 0)
        self._test_checkout()


class CheckoutBase(TestDvcGit):
    GIT_IGNORE = ".gitignore"

    def commit_data_file(self, fname, content="random text"):
        with open(fname, "w") as fd:
            fd.write(content)
        stages = self.dvc.add(fname)
        self.assertEqual(len(stages), 1)
        self.assertTrue(stages[0] is not None)
        self.dvc.scm.add([fname + ".dvc", ".gitignore"])
        self.dvc.scm.commit("adding " + fname)

    def read_ignored(self):
        with open(self.GIT_IGNORE) as f:
            return [s.strip("\n") for s in f.readlines()]

    def outs_info(self, stage):
        FileInfo = collections.namedtuple("FileInfo", "path inode")

        paths = [
            path
            for output in stage["outs"]
            for path in self.dvc.tree.walk_files(output["path"])
        ]

        return [
            FileInfo(path=path, inode=System.inode(path)) for path in paths
        ]


class TestRemoveFilesWhenCheckout(CheckoutBase):
    def test(self):
        fname = "file_in_a_branch"
        branch_master = "master"
        branch_1 = "b1"

        self.dvc.scm.add(self.dvc.scm.untracked_files())
        self.dvc.scm.commit("add all files")

        # add the file into a separate branch
        self.dvc.scm.checkout(branch_1, True)
        ret = main(["checkout", "--force"])
        self.assertEqual(ret, 0)
        self.commit_data_file(fname)

        # Checkout back in master
        self.dvc.scm.checkout(branch_master)
        self.assertTrue(os.path.exists(fname))

        # Make sure `dvc checkout` removes the file
        # self.dvc.checkout()
        ret = main(["checkout", "--force"])
        self.assertEqual(ret, 0)
        self.assertFalse(os.path.exists(fname))


class TestCheckoutCleanWorkingDir(CheckoutBase):
    @patch("dvc.prompt.confirm", return_value=True)
    def test(self, mock_prompt):
        mock_prompt.return_value = True

        stages = self.dvc.add(self.DATA_DIR)
        stage = stages[0]

        working_dir_change = os.path.join(self.DATA_DIR, "not_cached.txt")
        with open(working_dir_change, "w") as f:
            f.write("not_cached")

        ret = main(["checkout", stage.relpath])
        self.assertEqual(ret, 0)
        self.assertFalse(os.path.exists(working_dir_change))

    @patch("dvc.prompt.confirm", return_value=False)
    def test_force(self, mock_prompt):
        mock_prompt.return_value = False

        stages = self.dvc.add(self.DATA_DIR)
        self.assertEqual(len(stages), 1)
        stage = stages[0]

        working_dir_change = os.path.join(self.DATA_DIR, "not_cached.txt")
        with open(working_dir_change, "w") as f:
            f.write("not_cached")

        ret = main(["checkout", stage.relpath])
        self.assertNotEqual(ret, 0)

        mock_prompt.assert_called()
        self.assertNotEqual(ret, 0)
        self.assertRaises(DvcException)


class TestCheckoutSelectiveRemove(CheckoutBase):
    def test(self):
        # Use copy to test for changes in the inodes
        ret = main(["config", "cache.type", "copy"])
        self.assertEqual(ret, 0)

        ret = main(["add", self.DATA_DIR])
        self.assertEqual(0, ret)

        stage_path = self.DATA_DIR + DVC_FILE_SUFFIX
        stage = load_yaml(stage_path)
        staged_files = self.outs_info(stage)

        # move instead of remove, to lock inode assigned to stage_files[0].path
        # if we were to use remove, we might end up with same inode assigned to
        # newly checked out file
        shutil.move(staged_files[0].path, "random_name")

        ret = main(["checkout", "--force", stage_path])
        self.assertEqual(ret, 0)

        checkedout_files = self.outs_info(stage)

        self.assertEqual(len(staged_files), len(checkedout_files))
        self.assertEqual(staged_files[0].path, checkedout_files[0].path)
        self.assertNotEqual(staged_files[0].inode, checkedout_files[0].inode)
        self.assertEqual(staged_files[1].inode, checkedout_files[1].inode)


class TestGitIgnoreBasic(CheckoutBase):
    def test(self):
        fname1 = "file_1"
        fname2 = "file_2"
        fname3 = "file_3"

        self.dvc.scm.add(self.dvc.scm.untracked_files())
        self.dvc.scm.commit("add all files")

        self.assertFalse(os.path.exists(self.GIT_IGNORE))

        self.commit_data_file(fname1)
        self.commit_data_file(fname2)
        self.dvc.run(
            single_stage=True,
            cmd=f"python {self.CODE} {self.FOO} {fname3}",
            deps=[self.CODE, self.FOO],
            outs_no_cache=[fname3],
        )

        self.assertTrue(os.path.exists(self.GIT_IGNORE))

        ignored = self.read_ignored()

        self.assertEqual(len(ignored), 2)

        self.assertIn("/" + fname1, ignored)
        self.assertIn("/" + fname2, ignored)


class TestGitIgnoreWhenCheckout(CheckoutBase):
    def test(self):
        fname_master = "file_in_a_master"
        branch_master = "master"
        fname_branch = "file_in_a_branch"
        branch_1 = "b1"

        self.dvc.scm.add(self.dvc.scm.untracked_files())
        self.dvc.scm.commit("add all files")
        self.commit_data_file(fname_master)

        self.dvc.scm.checkout(branch_1, True)
        ret = main(["checkout", "--force"])
        self.assertEqual(ret, 0)
        self.commit_data_file(fname_branch)

        self.dvc.scm.checkout(branch_master)
        ret = main(["checkout", "--force"])
        self.assertEqual(ret, 0)

        ignored = self.read_ignored()

        self.assertEqual(len(ignored), 1)
        self.assertIn("/" + fname_master, ignored)

        self.dvc.scm.checkout(branch_1)
        ret = main(["checkout", "--force"])
        self.assertEqual(ret, 0)
        ignored = self.read_ignored()
        self.assertIn("/" + fname_branch, ignored)


class TestCheckoutMissingMd5InStageFile(TestRepro):
    def test(self):
        d = load_yaml(self.file1_stage)
        del d[Stage.PARAM_OUTS][0][LocalRemoteTree.PARAM_CHECKSUM]
        del d[Stage.PARAM_DEPS][0][LocalRemoteTree.PARAM_CHECKSUM]
        dump_yaml(self.file1_stage, d)

        with pytest.raises(CheckoutError):
            self.dvc.checkout(force=True)


class TestCheckoutEmptyDir(TestDvc):
    def test(self):
        dname = "empty_dir"
        os.mkdir(dname)

        stages = self.dvc.add(dname)
        self.assertEqual(len(stages), 1)
        stage = stages[0]
        self.assertTrue(stage is not None)
        self.assertEqual(len(stage.outs), 1)

        stage.outs[0].remove()
        self.assertFalse(os.path.exists(dname))

        stats = self.dvc.checkout(force=True)
        assert stats["added"] == [dname + os.sep]

        self.assertTrue(os.path.isdir(dname))
        self.assertEqual(len(os.listdir(dname)), 0)


class TestCheckoutNotCachedFile(TestDvc):
    def test(self):
        cmd = "python {} {} {}".format(self.CODE, self.FOO, "out")

        self.dvc.add(self.FOO)
        stage = self.dvc.run(
            cmd=cmd,
            deps=[self.FOO, self.CODE],
            outs_no_cache=["out"],
            single_stage=True,
        )
        self.assertTrue(stage is not None)

        stats = self.dvc.checkout(force=True)
        assert not any(stats.values())


class TestCheckoutWithDeps(TestRepro):
    def test(self):
        os.unlink(self.FOO)
        os.unlink(self.file1)

        self.assertFalse(os.path.exists(self.FOO))
        self.assertFalse(os.path.exists(self.file1))

        ret = main(["checkout", "--force", self.file1_stage, "--with-deps"])
        self.assertEqual(ret, 0)

        self.assertTrue(os.path.exists(self.FOO))
        self.assertTrue(os.path.exists(self.file1))


class TestCheckoutDirectory(TestRepro):
    def test(self):
        stage = self.dvc.add(self.DATA_DIR)[0]

        shutil.rmtree(self.DATA_DIR)
        self.assertFalse(os.path.exists(self.DATA_DIR))

        ret = main(["checkout", stage.path])
        self.assertEqual(ret, 0)

        self.assertTrue(os.path.exists(self.DATA_DIR))


class TestCheckoutHook(TestDvc):
    @patch("sys.stdout.isatty", return_value=True)
    @patch("dvc.prompt.input", side_effect=EOFError)
    def test(self, mock_input, mock_isatty):
        """ Test that dvc checkout handles EOFError gracefully, which is what
        it will experience when running in a git hook.
        """
        stages = self.dvc.add(self.DATA_DIR)
        self.assertEqual(len(stages), 1)
        stage = stages[0]
        self.assertNotEqual(stage, None)

        self.create(os.path.join(self.DATA_DIR, "test"), "test")

        with self.assertRaises(ConfirmRemoveError):
            self.dvc.checkout()


class TestCheckoutSuggestGit(TestRepro):
    def test(self):

        try:
            self.dvc.checkout(targets="gitbranch")
        except DvcException as exc:
            self.assertIsInstance(exc, CheckoutErrorSuggestGit)
            self.assertIsInstance(exc.__cause__, NoOutputOrStageError)
            self.assertIsInstance(
                exc.__cause__.__cause__, StageFileDoesNotExistError
            )

        try:
            self.dvc.checkout(targets=self.FOO)
        except DvcException as exc:
            self.assertIsInstance(exc, CheckoutErrorSuggestGit)
            self.assertIsInstance(exc.__cause__, NoOutputOrStageError)
            self.assertIsNone(exc.__cause__.__cause__)

        try:
            self.dvc.checkout(targets="looks-like-dvcfile.dvc")
        except DvcException as exc:
            self.assertIsInstance(exc, CheckoutErrorSuggestGit)
            self.assertIsInstance(exc.__cause__, StageFileDoesNotExistError)
            self.assertIsNone(exc.__cause__.__cause__)


class TestCheckoutTargetRecursiveShouldNotRemoveOtherUsedFiles(TestDvc):
    def test(self):
        ret = main(["add", self.DATA_DIR, self.FOO, self.BAR])
        self.assertEqual(0, ret)

        ret = main(["checkout", "-R", self.DATA_DIR])
        self.assertEqual(0, ret)

        self.assertTrue(os.path.exists(self.FOO))
        self.assertTrue(os.path.exists(self.BAR))


class TestCheckoutRecursiveNotDirectory(TestDvc):
    def test(self):
        ret = main(["add", self.FOO])
        self.assertEqual(0, ret)

        stats = self.dvc.checkout(targets=[self.FOO + ".dvc"], recursive=True)
        assert stats == {"added": [], "modified": [], "deleted": []}


class TestCheckoutMovedCacheDirWithSymlinks(TestDvc):
    def test(self):
        ret = main(["config", "cache.type", "symlink"])
        self.assertEqual(ret, 0)

        ret = main(["add", self.FOO])
        self.assertEqual(ret, 0)

        ret = main(["add", self.DATA_DIR])
        self.assertEqual(ret, 0)

        self.assertTrue(System.is_symlink(self.FOO))
        old_foo_link = os.path.realpath(self.FOO)

        self.assertTrue(System.is_symlink(self.DATA))
        old_data_link = os.path.realpath(self.DATA)

        old_cache_dir = self.dvc.cache.local.cache_dir
        new_cache_dir = old_cache_dir + "_new"
        os.rename(old_cache_dir, new_cache_dir)

        ret = main(["cache", "dir", new_cache_dir])
        self.assertEqual(ret, 0)

        ret = main(["checkout", "-f"])
        self.assertEqual(ret, 0)

        self.assertTrue(System.is_symlink(self.FOO))
        new_foo_link = os.path.realpath(self.FOO)

        self.assertTrue(System.is_symlink(self.DATA))
        new_data_link = os.path.realpath(self.DATA)

        self.assertEqual(
            relpath(old_foo_link, old_cache_dir),
            relpath(new_foo_link, new_cache_dir),
        )

        self.assertEqual(
            relpath(old_data_link, old_cache_dir),
            relpath(new_data_link, new_cache_dir),
        )


def test_checkout_no_checksum(tmp_dir, dvc):
    tmp_dir.gen("file", "file content")
    stage = dvc.run(
        outs=["file"], no_exec=True, cmd="somecmd", single_stage=True
    )

    with pytest.raises(CheckoutError):
        dvc.checkout([stage.path], force=True)

    assert not os.path.exists("file")


@pytest.mark.parametrize(
    "link, link_test_func",
    [("hardlink", System.is_hardlink), ("symlink", System.is_symlink)],
)
def test_checkout_relink(tmp_dir, dvc, link, link_test_func):
    dvc.cache.local.cache_types = [link]

    tmp_dir.dvc_gen({"dir": {"data": "text"}})
    dvc.unprotect("dir/data")
    assert not link_test_func("dir/data")

    stats = dvc.checkout(["dir.dvc"], relink=True)
    assert stats == empty_checkout
    assert link_test_func("dir/data")


@pytest.mark.parametrize("link", ["hardlink", "symlink", "copy"])
def test_checkout_relink_protected(tmp_dir, dvc, link):
    dvc.cache.local.cache_types = [link]

    tmp_dir.dvc_gen("foo", "foo")
    dvc.unprotect("foo")
    assert os.access("foo", os.W_OK)

    stats = dvc.checkout(["foo.dvc"], relink=True)
    assert stats == empty_checkout

    # NOTE: Windows symlink perms don't propagate to the target
    if link == "copy" or (link == "symlink" and os.name == "nt"):
        assert os.access("foo", os.W_OK)
    else:
        assert not os.access("foo", os.W_OK)


@pytest.mark.parametrize(
    "target",
    [os.path.join("dir", "subdir"), os.path.join("dir", "subdir", "file")],
)
def test_partial_checkout(tmp_dir, dvc, target):
    tmp_dir.dvc_gen({"dir": {"subdir": {"file": "file"}, "other": "other"}})
    shutil.rmtree("dir")
    stats = dvc.checkout([target])
    assert stats["added"] == ["dir" + os.sep]
    assert list(walk_files("dir")) == [os.path.join("dir", "subdir", "file")]


empty_checkout = {"added": [], "deleted": [], "modified": []}


def test_stats_on_empty_checkout(tmp_dir, dvc, scm):
    assert dvc.checkout() == empty_checkout
    tmp_dir.dvc_gen(
        {"dir": {"subdir": {"file": "file"}, "other": "other"}},
        commit="initial",
    )
    assert dvc.checkout() == empty_checkout


def test_stats_on_checkout(tmp_dir, dvc, scm):
    tmp_dir.dvc_gen(
        {
            "dir": {"subdir": {"file": "file"}, "other": "other"},
            "foo": "foo",
            "bar": "bar",
        },
        commit="initial",
    )
    scm.checkout("HEAD~")
    stats = dvc.checkout()
    assert set(stats["deleted"]) == {"dir" + os.sep, "foo", "bar"}

    scm.checkout("-")
    stats = dvc.checkout()
    assert set(stats["added"]) == {"bar", "dir" + os.sep, "foo"}

    tmp_dir.gen({"lorem": "lorem", "bar": "new bar", "dir2": {"file": "file"}})
    (tmp_dir / "foo").unlink()
    scm.repo.git.rm("foo.dvc")
    tmp_dir.dvc_add(["bar", "lorem", "dir2"], commit="second")

    scm.checkout("HEAD~")
    stats = dvc.checkout()
    assert set(stats["modified"]) == {"bar"}
    assert set(stats["added"]) == {"foo"}
    assert set(stats["deleted"]) == {"lorem", "dir2" + os.sep}

    scm.checkout("-")
    stats = dvc.checkout()
    assert set(stats["modified"]) == {"bar"}
    assert set(stats["added"]) == {"dir2" + os.sep, "lorem"}
    assert set(stats["deleted"]) == {"foo"}


def test_checkout_stats_on_failure(tmp_dir, dvc, scm):
    tmp_dir.dvc_gen(
        {"foo": "foo", "dir": {"subdir": {"file": "file"}}, "other": "other"},
        commit="initial",
    )
    stage = Dvcfile(dvc, "foo.dvc").stage
    tmp_dir.dvc_gen({"foo": "foobar", "other": "other other"}, commit="second")

    # corrupt cache
    cache = stage.outs[0].cache_path
    os.chmod(cache, 0o644)
    with open(cache, "a") as fd:
        fd.write("destroy cache")

    scm.checkout("HEAD~")
    with pytest.raises(CheckoutError) as exc:
        dvc.checkout(force=True)

    assert exc.value.stats == {
        **empty_checkout,
        "failed": ["foo"],
        "modified": ["other"],
    }


def test_stats_on_added_file_from_tracked_dir(tmp_dir, dvc, scm):
    tmp_dir.dvc_gen(
        {"dir": {"subdir": {"file": "file"}, "other": "other"}},
        commit="initial",
    )

    tmp_dir.gen("dir/subdir/newfile", "newfile")
    tmp_dir.dvc_add("dir", commit="add newfile")
    scm.checkout("HEAD~")
    assert dvc.checkout() == {**empty_checkout, "modified": ["dir" + os.sep]}
    assert dvc.checkout() == empty_checkout

    scm.checkout("-")
    assert dvc.checkout() == {**empty_checkout, "modified": ["dir" + os.sep]}
    assert dvc.checkout() == empty_checkout


def test_stats_on_updated_file_from_tracked_dir(tmp_dir, dvc, scm):
    tmp_dir.dvc_gen(
        {"dir": {"subdir": {"file": "file"}, "other": "other"}},
        commit="initial",
    )

    tmp_dir.gen("dir/subdir/file", "what file?")
    tmp_dir.dvc_add("dir", commit="update file")
    scm.checkout("HEAD~")
    assert dvc.checkout() == {**empty_checkout, "modified": ["dir" + os.sep]}
    assert dvc.checkout() == empty_checkout

    scm.checkout("-")
    assert dvc.checkout() == {**empty_checkout, "modified": ["dir" + os.sep]}
    assert dvc.checkout() == empty_checkout


def test_stats_on_removed_file_from_tracked_dir(tmp_dir, dvc, scm):
    tmp_dir.dvc_gen(
        {"dir": {"subdir": {"file": "file"}, "other": "other"}},
        commit="initial",
    )

    (tmp_dir / "dir" / "subdir" / "file").unlink()
    tmp_dir.dvc_add("dir", commit="removed file from subdir")
    scm.checkout("HEAD~")
    assert dvc.checkout() == {**empty_checkout, "modified": ["dir" + os.sep]}
    assert dvc.checkout() == empty_checkout

    scm.checkout("-")
    assert dvc.checkout() == {**empty_checkout, "modified": ["dir" + os.sep]}
    assert dvc.checkout() == empty_checkout


def test_stats_on_show_changes_does_not_show_summary(
    tmp_dir, dvc, scm, caplog
):
    tmp_dir.dvc_gen(
        {"dir": {"subdir": {"file": "file"}}, "other": "other"},
        commit="initial",
    )
    scm.checkout("HEAD~")

    with caplog.at_level(logging.INFO, logger="dvc"):
        caplog.clear()
        assert main(["checkout"]) == 0
        for out in ["D\tdir" + os.sep, "D\tother"]:
            assert out in caplog.text
        assert "modified" not in caplog.text
        assert "deleted" not in caplog.text
        assert "added" not in caplog.text


def test_stats_does_not_show_changes_by_default(tmp_dir, dvc, scm, caplog):
    tmp_dir.dvc_gen(
        {"dir": {"subdir": {"file": "file"}}, "other": "other"},
        commit="initial",
    )
    scm.checkout("HEAD~")

    with caplog.at_level(logging.INFO, logger="dvc"):
        caplog.clear()
        assert main(["checkout", "--summary"]) == 0
        assert "2 files deleted" in caplog.text
        assert "dir" not in caplog.text
        assert "other" not in caplog.text


@pytest.mark.parametrize("link", ["hardlink", "symlink", "copy"])
def test_checkout_with_relink_existing(tmp_dir, dvc, link):
    tmp_dir.dvc_gen("foo", "foo")
    (tmp_dir / "foo").unlink()

    tmp_dir.dvc_gen("bar", "bar")
    dvc.cache.local.cache_types = [link]

    stats = dvc.checkout(relink=True)
    assert stats == {**empty_checkout, "added": ["foo"]}


def test_checkout_with_deps(tmp_dir, dvc):
    tmp_dir.dvc_gen({"foo": "foo"})
    dvc.run(
        fname="copy_file.dvc",
        cmd="echo foo > bar",
        outs=["bar"],
        deps=["foo"],
        single_stage=True,
    )

    (tmp_dir / "bar").unlink()
    (tmp_dir / "foo").unlink()

    stats = dvc.checkout(["copy_file.dvc"], with_deps=False)
    assert stats == {**empty_checkout, "added": ["bar"]}

    (tmp_dir / "bar").unlink()
    stats = dvc.checkout(["copy_file.dvc"], with_deps=True)
    assert set(stats["added"]) == {"foo", "bar"}


def test_checkout_recursive(tmp_dir, dvc):
    tmp_dir.gen({"dir": {"foo": "foo", "bar": "bar"}})
    dvc.add("dir", recursive=True)

    (tmp_dir / "dir" / "foo").unlink()
    (tmp_dir / "dir" / "bar").unlink()

    stats = dvc.checkout(["dir"], recursive=True)
    assert set(stats["added"]) == {
        os.path.join("dir", "foo"),
        os.path.join("dir", "bar"),
    }


@pytest.mark.skipif(
    not S3.should_test(), reason="Only run with S3 credentials"
)
def test_checkout_for_external_outputs(tmp_dir, dvc):
    dvc.cache.s3 = CloudCache(S3RemoteTree(dvc, {"url": S3.get_url()}))

    remote = Remote(S3RemoteTree(dvc, {"url": S3.get_url()}))
    file_path = remote.path_info / "foo"
    remote.tree.s3.put_object(
        Bucket=remote.path_info.bucket, Key=file_path.path, Body="foo"
    )

    dvc.add(str(remote.path_info / "foo"), external=True)

    remote.tree.remove(file_path)
    stats = dvc.checkout(force=True)
    assert stats == {**empty_checkout, "added": [str(file_path)]}
    assert remote.tree.exists(file_path)

    remote.tree.s3.put_object(
        Bucket=remote.path_info.bucket, Key=file_path.path, Body="foo\nfoo"
    )
    stats = dvc.checkout(force=True)
    assert stats == {**empty_checkout, "modified": [str(file_path)]}


def test_checkouts_with_different_addressing(tmp_dir, dvc, run_copy):
    tmp_dir.gen({"foo": "foo", "lorem": "lorem"})
    run_copy("foo", "bar", name="copy-foo-bar")
    run_copy("lorem", "ipsum", name="copy-lorem-ipsum")

    (tmp_dir / "bar").unlink()
    (tmp_dir / "ipsum").unlink()
    assert set(dvc.checkout(PIPELINE_FILE)["added"]) == {"bar", "ipsum"}

    (tmp_dir / "bar").unlink()
    (tmp_dir / "ipsum").unlink()
    assert set(dvc.checkout(":")["added"]) == {"bar", "ipsum"}

    (tmp_dir / "bar").unlink()
    assert dvc.checkout("copy-foo-bar")["added"] == ["bar"]

    (tmp_dir / "bar").unlink()
    assert dvc.checkout("dvc.yaml:copy-foo-bar")["added"] == ["bar"]

    (tmp_dir / "bar").unlink()
    assert dvc.checkout(":copy-foo-bar")["added"] == ["bar"]

    (tmp_dir / "bar").unlink()
    (tmp_dir / "data").mkdir()
    with (tmp_dir / "data").chdir():
        assert dvc.checkout(relpath(tmp_dir / "dvc.yaml") + ":copy-foo-bar")[
            "added"
        ] == [relpath(tmp_dir / "bar")]

    (tmp_dir / "bar").unlink()
    assert dvc.checkout("bar")["added"] == ["bar"]


def test_checkouts_on_same_stage_name_and_output_name(tmp_dir, dvc, run_copy):
    tmp_dir.gen("foo", "foo")
    run_copy("foo", "bar", name="copy-foo-bar")
    run_copy("foo", "copy-foo-bar", name="make_collision")

    (tmp_dir / "bar").unlink()
    (tmp_dir / "copy-foo-bar").unlink()
    assert dvc.checkout("copy-foo-bar")["added"] == ["bar"]
    assert dvc.checkout("./copy-foo-bar")["added"] == ["copy-foo-bar"]


def test_checkouts_for_pipeline_tracked_outs(tmp_dir, dvc, scm, run_copy):
    tmp_dir.gen("foo", "foo")
    stage1 = run_copy("foo", "bar", name="copy-foo-bar")
    tmp_dir.gen("lorem", "lorem")
    stage2 = run_copy("lorem", "ipsum", name="copy-lorem-ipsum")

    for out in ["bar", "ipsum"]:
        (tmp_dir / out).unlink()
    assert dvc.checkout(["bar"])["added"] == ["bar"]

    (tmp_dir / "bar").unlink()
    assert set(dvc.checkout([PIPELINE_FILE])["added"]) == {"bar", "ipsum"}

    for out in ["bar", "ipsum"]:
        (tmp_dir / out).unlink()
    assert set(dvc.checkout([stage1.addressing])["added"]) == {"bar"}

    (tmp_dir / "bar").unlink()
    assert set(dvc.checkout([stage2.addressing])["added"]) == {"ipsum"}

    (tmp_dir / "ipsum").unlink()
    assert set(dvc.checkout()["added"]) == {"bar", "ipsum"}
