import logging

from funcy import first, project

from dvc.exceptions import DvcException, NoPlotsError, OutputNotFoundError
from dvc.repo.tree import RepoTree
from dvc.schema import PLOT_PROPS
from dvc.utils import relpath

logger = logging.getLogger(__name__)


class NotAPlotError(DvcException):
    def __init__(self, out):
        super().__init__(
            f"'{out}' is not a plot. Use `dvc plots modify` to change that."
        )


class PropsNotFoundError(DvcException):
    pass


class Plots:
    def __init__(self, repo):
        self.repo = repo

    def collect(self, targets=None, revs=None):
        """Collects all props and data for plots.

        Returns a structure like:
            {rev: {plots.csv: {
                props: {x: ..., "header": ..., ...},
                data: "...data as a string...",
            }}}
        Data parsing is postponed, since it's affected by props.
        """
        targets = [targets] if isinstance(targets, str) else targets or []
        data = {}
        for rev in self.repo.brancher(revs=revs):
            # .brancher() adds unwanted workspace
            if revs is not None and rev not in revs:
                continue
            rev = rev or "workspace"

            tree = RepoTree(self.repo)
            plots = _collect_plots(self.repo, targets, rev)
            for path_info, props in plots.items():
                datafile = relpath(path_info, self.repo.root_dir)
                if rev not in data:
                    data[rev] = {}
                data[rev].update({datafile: {"props": props}})

                # Load data from git or dvc cache
                try:
                    with tree.open(path_info) as fd:
                        data[rev][datafile]["data"] = fd.read()
                except FileNotFoundError as e:
                    # This might happen simply because cache is absent
                    print(e)
                    pass

        return data

    @staticmethod
    def render(data, revs=None, props=None, templates=None):
        """Renders plots"""
        props = props or {}

        # Merge data by plot file and apply overriding props
        plots = _prepare_plots(data, revs, props)

        return {
            datafile: _render(datafile, desc["data"], desc["props"], templates)
            for datafile, desc in plots.items()
        }

    def show(self, targets=None, revs=None, props=None):
        from .data import NoMetricInHistoryError

        data = self.collect(targets, revs)

        # If any mentioned plot doesn't have any data then that's an error
        targets = [targets] if isinstance(targets, str) else targets or []
        for target in targets:
            if not any("data" in d[target] for d in data.values()):
                raise NoMetricInHistoryError(target)

        # No data at all is a special error with a special message
        if not data:
            raise NoPlotsError()

        return self.render(data, revs, props, self.repo.plot_templates)

    def diff(self, *args, **kwargs):
        from .diff import diff

        return diff(self.repo, *args, **kwargs)

    @staticmethod
    def _unset(out, props):
        missing = list(set(props) - set(out.plot.keys()))
        if missing:
            raise PropsNotFoundError(
                f"display properties {missing} not found in plot '{out}'"
            )

        for prop in props:
            out.plot.pop(prop)

    def modify(self, path, props=None, unset=None):
        from dvc.dvcfile import Dvcfile

        props = props or {}
        template = props.get("template")
        if template:
            self.repo.plot_templates.get_template(template)

        (out,) = self.repo.find_outs_by_path(path)
        if not out.plot and unset is not None:
            raise NotAPlotError(out)

        # This out will become a plot unless it is one already
        if not isinstance(out.plot, dict):
            out.plot = {}

        if unset:
            self._unset(out, unset)

        out.plot.update(props)

        # Empty dict will move it to non-plots
        if not out.plot:
            out.plot = True

        out.verify_metric()

        dvcfile = Dvcfile(self.repo, out.stage.path)
        dvcfile.dump(out.stage, update_pipeline=True, no_lock=True)


def _collect_plots(repo, targets=None, rev=None):
    def _targets_to_outs(targets):
        for t in targets:
            try:
                (out,) = repo.find_outs_by_path(t)
                yield out
            except OutputNotFoundError:
                logger.warning(
                    "File '{}' was not found at: '{}'. It will not be "
                    "plotted.".format(t, rev)
                )

    if targets:
        outs = _targets_to_outs(targets)
    else:
        outs = (out for stage in repo.stages for out in stage.outs if out.plot)

    return {out.path_info: _plot_props(out) for out in outs}


def _plot_props(out):
    if not out.plot:
        raise NotAPlotError(out)
    if isinstance(out.plot, list):
        raise DvcException("Multiple plots per data file not supported.")
    if isinstance(out.plot, bool):
        return {}

    return project(out.plot, PLOT_PROPS)


def _prepare_plots(data, revs, props):
    """Groups data by plot file.

    Also resolves props conflicts between revs and applies global props.
    """
    # we go in order revs are supplied on props conflict first ones win.
    revs = iter(data) if revs is None else revs

    plots, props_revs = {}, {}
    for rev in revs:
        # Asked for revision without data
        if rev not in data:
            continue

        for datafile, desc in data[rev].items():
            # props from command line overwrite plot props from out definition
            full_props = {**desc["props"], **props}

            if datafile in plots:
                saved = plots[datafile]
                if saved["props"] != full_props:
                    logger.warning(
                        f"Inconsistent plot props for '{datafile}' in "
                        f"'{props_revs[datafile]}' and '{rev}'. "
                        f"Going to use ones from '{props_revs[datafile]}'"
                    )

                saved["data"][rev] = desc["data"]
            else:
                plots[datafile] = {
                    "props": full_props,
                    "data": {rev: desc["data"]},
                }
                # Save rev we got props from
                props_revs[datafile] = rev

    return plots


def _render(datafile, datas, props, templates):
    from .data import plot_data, PlotData

    # Copy it to not modify a passed value
    props = props.copy()

    # Add x and y to fields if set
    fields = props.get("fields")
    if fields is not None:
        fields = {*fields, props.get("x"), props.get("y")} - {None}

    template = templates.load(props.get("template") or "default")

    # If x is not set add index field
    if not props.get("x") and template.has_anchor("x"):
        props["append_index"] = True
        props["x"] = PlotData.INDEX_FIELD

    # Parse all data, preprocess it and collect as a list of dicts
    data = []
    for rev, datablob in datas.items():
        rev_data = plot_data(datafile, rev, datablob).to_datapoints(
            fields=fields,
            path=props.get("path"),
            header=props.get("header", True),
            append_index=props.get("append_index", False),
        )
        data.extend(rev_data)

    # If y is not set then use last field not used yet
    if not props.get("y") and template.has_anchor("y"):
        fields = list(first(data))
        skip = (PlotData.REVISION_FIELD, props.get("x"))
        props["y"] = first(f for f in reversed(fields) if f not in skip)

    return template.render(data, props=props)
