from collections import OrderedDict
from functools import partial
from operator import attrgetter
from typing import TYPE_CHECKING, List

from funcy import post_processing

from dvc.dependency import ParamsDependency
from dvc.output import BaseOutput
from dvc.utils.collections import apply_diff
from dvc.utils.yaml import parse_yaml_for_update

from .params import StageParams
from .utils import resolve_wdir, split_params_deps

if TYPE_CHECKING:
    from dvc.stage import PipelineStage, Stage

PARAM_PARAMS = ParamsDependency.PARAM_PARAMS
PARAM_PATH = ParamsDependency.PARAM_PATH

PARAM_DEPS = StageParams.PARAM_DEPS
PARAM_OUTS = StageParams.PARAM_OUTS

PARAM_CACHE = BaseOutput.PARAM_CACHE
PARAM_METRIC = BaseOutput.PARAM_METRIC
PARAM_PLOT = BaseOutput.PARAM_PLOT
PARAM_PERSIST = BaseOutput.PARAM_PERSIST

DEFAULT_PARAMS_FILE = ParamsDependency.DEFAULT_PARAMS_FILE


sort_by_path = partial(sorted, key=attrgetter("def_path"))


@post_processing(OrderedDict)
def _get_flags(out):
    if not out.use_cache:
        yield PARAM_CACHE, False
    if out.persist:
        yield PARAM_PERSIST, True
    if out.plot and isinstance(out.plot, dict):
        # notice `out.plot` is not sorted
        # `out.plot` is in the same order as is in the file when read
        # and, should be dumped as-is without any sorting
        yield from out.plot.items()


def _serialize_out(out):
    flags = _get_flags(out)
    return out.def_path if not flags else {out.def_path: flags}


def _serialize_outs(outputs: List[BaseOutput]):
    outs, metrics, plots = [], [], []
    for out in sort_by_path(outputs):
        bucket = outs
        if out.plot:
            bucket = plots
        elif out.metric:
            bucket = metrics
        bucket.append(_serialize_out(out))
    return outs, metrics, plots


def _serialize_params_keys(params):
    """
    Returns the following format of data:
     ['lr', 'train', {'params2.yaml': ['lr']}]

    The output is sorted, with keys of params from default params file being
    at the first, and then followed by entry of other files in lexicographic
    order. The keys of those custom files are also sorted in the same order.
    """
    keys = []
    for param_dep in sort_by_path(params):
        dump = param_dep.dumpd()
        path, params = dump[PARAM_PATH], dump[PARAM_PARAMS]
        assert isinstance(params, (dict, list))
        # when on no_exec, params are not filled and are saved as list
        k = sorted(params.keys() if isinstance(params, dict) else params)
        if not k:
            continue

        if path == DEFAULT_PARAMS_FILE:
            keys = k + keys
        else:
            keys.append({path: k})
    return keys


def _serialize_params_values(params: List[ParamsDependency]):
    """Returns output of following format, used for lockfile:
        {'params.yaml': {'lr': '1', 'train': 2}, {'params2.yaml': {'lr': '1'}}

    Default params file are always kept at the start, followed by others in
    alphabetical order. The param values are sorted too(not recursively though)
    """
    key_vals = OrderedDict()
    for param_dep in sort_by_path(params):
        dump = param_dep.dumpd()
        path, params = dump[PARAM_PATH], dump[PARAM_PARAMS]
        if isinstance(params, dict):
            kv = [(key, params[key]) for key in sorted(params.keys())]
            key_vals[path] = OrderedDict(kv)
            if path == DEFAULT_PARAMS_FILE:
                key_vals.move_to_end(path, last=False)
    return key_vals


def to_pipeline_file(stage: "PipelineStage"):
    wdir = resolve_wdir(stage.wdir, stage.path)
    params, deps = split_params_deps(stage)
    deps = sorted([d.def_path for d in deps])
    params = _serialize_params_keys(params)

    outs, metrics, plots = _serialize_outs(stage.outs)
    res = [
        (stage.PARAM_CMD, stage.cmd),
        (stage.PARAM_WDIR, wdir),
        (stage.PARAM_DEPS, deps),
        (stage.PARAM_PARAMS, params),
        (stage.PARAM_OUTS, outs),
        (stage.PARAM_METRICS, metrics),
        (stage.PARAM_PLOTS, plots),
        (stage.PARAM_FROZEN, stage.frozen),
        (stage.PARAM_ALWAYS_CHANGED, stage.always_changed),
    ]
    return {
        stage.name: OrderedDict([(key, value) for key, value in res if value])
    }


def to_single_stage_lockfile(stage: "Stage") -> dict:
    assert stage.cmd

    res = OrderedDict([("cmd", stage.cmd)])
    params, deps = split_params_deps(stage)
    deps, outs = [
        [
            OrderedDict(
                [
                    (PARAM_PATH, item.def_path),
                    (item.checksum_type, item.checksum),
                ]
            )
            for item in sort_by_path(items)
        ]
        for items in [deps, stage.outs]
    ]
    params = _serialize_params_values(params)
    if deps:
        res[PARAM_DEPS] = deps
    if params:
        res[PARAM_PARAMS] = params
    if outs:
        res[PARAM_OUTS] = outs

    return res


def to_lockfile(stage: "PipelineStage") -> dict:
    assert stage.name
    return {stage.name: to_single_stage_lockfile(stage)}


def to_single_stage_file(stage: "Stage"):
    state = stage.dumpd()

    # When we load a stage we parse yaml with a fast parser, which strips
    # off all the comments and formatting. To retain those on update we do
    # a trick here:
    # - reparse the same yaml text with a slow but smart ruamel yaml parser
    # - apply changes to a returned structure
    # - serialize it
    if stage._stage_text is not None:
        saved_state = parse_yaml_for_update(stage._stage_text, stage.path)
        # Stage doesn't work with meta in any way, so .dumpd() doesn't
        # have it. We simply copy it over.
        if "meta" in saved_state:
            state["meta"] = saved_state["meta"]
        apply_diff(state, saved_state)
        state = saved_state
    return state
