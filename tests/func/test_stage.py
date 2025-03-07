import os
import tempfile

import pytest

from dvc.dvcfile import SingleStageFile
from dvc.main import main
from dvc.output.local import LocalOutput
from dvc.remote.local import LocalRemoteTree
from dvc.repo import Repo
from dvc.stage import PipelineStage, Stage
from dvc.stage.exceptions import StageFileFormatError
from dvc.utils.yaml import dump_yaml, load_yaml
from tests.basic_env import TestDvc


def test_cmd_obj():
    with pytest.raises(StageFileFormatError):
        SingleStageFile.validate({Stage.PARAM_CMD: {}})


def test_cmd_none():
    SingleStageFile.validate({Stage.PARAM_CMD: None})


def test_no_cmd():
    SingleStageFile.validate({})


def test_cmd_str():
    SingleStageFile.validate({Stage.PARAM_CMD: "cmd"})


def test_object():
    with pytest.raises(StageFileFormatError):
        SingleStageFile.validate({Stage.PARAM_DEPS: {}})

    with pytest.raises(StageFileFormatError):
        SingleStageFile.validate({Stage.PARAM_OUTS: {}})


def test_none():
    SingleStageFile.validate({Stage.PARAM_DEPS: None})
    SingleStageFile.validate({Stage.PARAM_OUTS: None})


def test_empty_list():
    d = {Stage.PARAM_DEPS: []}
    SingleStageFile.validate(d)

    d = {Stage.PARAM_OUTS: []}
    SingleStageFile.validate(d)


def test_list():
    lst = [
        {LocalOutput.PARAM_PATH: "foo", LocalRemoteTree.PARAM_CHECKSUM: "123"},
        {LocalOutput.PARAM_PATH: "bar", LocalRemoteTree.PARAM_CHECKSUM: None},
        {LocalOutput.PARAM_PATH: "baz"},
    ]
    d = {Stage.PARAM_DEPS: lst}
    SingleStageFile.validate(d)

    lst[0][LocalOutput.PARAM_CACHE] = True
    lst[1][LocalOutput.PARAM_CACHE] = False
    d = {Stage.PARAM_OUTS: lst}
    SingleStageFile.validate(d)


class TestReload(TestDvc):
    def test(self):
        stages = self.dvc.add(self.FOO)
        self.assertEqual(len(stages), 1)
        stage = stages[0]
        self.assertTrue(stage is not None)

        d = load_yaml(stage.relpath)

        # NOTE: checking that reloaded stage didn't change its checksum
        md5 = "11111111111111111111111111111111"
        d[stage.PARAM_MD5] = md5
        dump_yaml(stage.relpath, d)

        dvcfile = SingleStageFile(self.dvc, stage.relpath)
        stage = dvcfile.stage

        self.assertTrue(stage is not None)
        dvcfile.dump(stage)

        d = load_yaml(stage.relpath)
        self.assertEqual(d[stage.PARAM_MD5], md5)


class TestDefaultWorkingDirectory(TestDvc):
    def test_ignored_in_checksum(self):
        stage = self.dvc.run(
            cmd=f"echo test > {self.FOO}",
            deps=[self.BAR],
            outs=[self.FOO],
            single_stage=True,
        )

        d = stage.dumpd()
        self.assertNotIn(Stage.PARAM_WDIR, d.keys())

        d = load_yaml(stage.relpath)
        self.assertNotIn(Stage.PARAM_WDIR, d.keys())

        with self.dvc.lock, self.dvc.state:
            stage = SingleStageFile(self.dvc, stage.relpath).stage
            self.assertFalse(stage.changed())


class TestExternalRemoteResolution(TestDvc):
    def test_remote_output(self):
        tmp_path = tempfile.mkdtemp()
        storage = os.path.join(tmp_path, "storage")
        file_path = os.path.join(storage, "file")

        os.makedirs(storage)

        assert main(["remote", "add", "tmp", tmp_path]) == 0
        assert main(["remote", "add", "storage", "remote://tmp/storage"]) == 0
        assert (
            main(
                [
                    "run",
                    "--single-stage",
                    "-O",
                    "remote://storage/file",
                    f"echo file > {file_path}",
                ]
            )
            == 0
        )

        assert os.path.exists(file_path)

    def test_remote_dependency(self):
        tmp_path = tempfile.mkdtemp()
        storage = os.path.join(tmp_path, "storage")
        file_path = os.path.join(storage, "file")

        os.makedirs(storage)

        with open(file_path, "w") as fobj:
            fobj.write("Isle of Dogs")

        assert main(["remote", "add", "tmp", tmp_path]) == 0
        assert main(["remote", "add", "storage", "remote://tmp/storage"]) == 0
        assert main(["import-url", "remote://storage/file", "movie.txt"]) == 0

        assert os.path.exists("movie.txt")


def test_md5_ignores_comments(tmp_dir, dvc):
    (stage,) = tmp_dir.dvc_gen("foo", "foo content")

    with open(stage.path, "a") as f:
        f.write("# End comment\n")

    new_stage = SingleStageFile(dvc, stage.path).stage
    assert not new_stage.changed_stage()


def test_meta_is_preserved(tmp_dir, dvc):
    (stage,) = tmp_dir.dvc_gen("foo", "foo content")

    # Add meta to DVC-file
    data = load_yaml(stage.path)
    data["meta"] = {"custom_key": 42}
    dump_yaml(stage.path, data)

    # Loading and dumping to test that it works and meta is retained
    dvcfile = SingleStageFile(dvc, stage.path)
    new_stage = dvcfile.stage
    dvcfile.dump(new_stage)

    new_data = load_yaml(stage.path)
    assert new_data["meta"] == data["meta"]


def test_parent_repo_collect_stages(tmp_dir, scm, dvc):
    tmp_dir.gen({"subdir": {}})
    tmp_dir.gen({"deep": {"dir": {}}})
    subrepo_dir = tmp_dir / "subdir"
    deep_subrepo_dir = tmp_dir / "deep" / "dir"

    with subrepo_dir.chdir():
        subrepo = Repo.init(subdir=True)
        subrepo_dir.gen("subrepo_file", "subrepo file content")
        subrepo.add("subrepo_file")

    with deep_subrepo_dir.chdir():
        deep_subrepo = Repo.init(subdir=True)
        deep_subrepo_dir.gen("subrepo_file", "subrepo file content")
        deep_subrepo.add("subrepo_file")

    stages = dvc.collect(None)
    subrepo_stages = subrepo.collect(None)
    deep_subrepo_stages = deep_subrepo.collect(None)

    assert stages == []
    assert subrepo_stages != []
    assert deep_subrepo_stages != []


def test_stage_strings_representation(tmp_dir, dvc, run_copy):
    tmp_dir.dvc_gen("foo", "foo")
    stage1 = run_copy("foo", "bar", single_stage=True)
    assert stage1.addressing == "bar.dvc"
    assert repr(stage1) == "Stage: 'bar.dvc'"
    assert str(stage1) == "stage: 'bar.dvc'"

    stage2 = run_copy("bar", "baz", name="copy-bar-baz")
    assert stage2.addressing == "copy-bar-baz"
    assert repr(stage2) == "Stage: 'copy-bar-baz'"
    assert str(stage2) == "stage: 'copy-bar-baz'"

    folder = tmp_dir / "dir"
    folder.mkdir()
    with folder.chdir():
        rel_path = os.path.relpath(stage1.path)
        assert stage1.addressing == rel_path
        assert repr(stage1) == f"Stage: '{rel_path}'"
        assert str(stage1) == f"stage: '{rel_path}'"

        rel_path = os.path.relpath(stage2.path)
        assert stage2.addressing == f"{rel_path}:{stage2.name}"
        assert repr(stage2) == f"Stage: '{rel_path}:{stage2.name}'"
        assert str(stage2) == f"stage: '{rel_path}:{stage2.name}'"


def test_stage_on_no_path_string_repr(tmp_dir, dvc):
    s = Stage(dvc)
    assert s.addressing == "No path"
    assert repr(s) == "Stage: 'No path'"
    assert str(s) == "stage: 'No path'"

    p = PipelineStage(dvc, name="stage_name")
    assert p.addressing == "No path:stage_name"
    assert repr(p) == "Stage: 'No path:stage_name'"
    assert str(p) == "stage: 'No path:stage_name'"


def test_stage_remove_pipeline_stage(tmp_dir, dvc, run_copy):
    tmp_dir.gen("foo", "foo")
    stage = run_copy("foo", "bar", name="copy-foo-bar")
    run_copy("bar", "foobar", name="copy-bar-foobar")

    dvc_file = stage.dvcfile
    with dvc.lock:
        stage.remove(purge=False)
    assert stage.name in dvc_file.stages

    with dvc.lock:
        stage.remove()
    assert stage.name not in dvc_file.stages
    assert "copy-bar-foobar" in dvc_file.stages


def test_stage_remove_pointer_stage(tmp_dir, dvc, run_copy):
    (stage,) = tmp_dir.dvc_gen("foo", "foo")

    with dvc.lock:
        stage.remove(purge=False)
    assert not (tmp_dir / "foo").exists()
    assert (tmp_dir / stage.relpath).exists()

    with dvc.lock:
        stage.remove()
    assert not (tmp_dir / stage.relpath).exists()
