"""StateForge structural feedback command line interface."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from stateforge.core.ir import ProgramIR
from stateforge.core.log import setup_logging
from stateforge.core.serialization import save_json, save_program_ir
from stateforge.runtime.io import merge_events
from stateforge.runtime.coverage import coverage
from stateforge.fuzzing.feedback.engine import compute_feedback
from stateforge.analysis.static.icfg import build_icfg


def analyze_cmd(target: str, out: str, callgraph_path: str | None = None) -> int:
    root = Path(target).resolve()
    callgraph = None
    if callgraph_path:
        callgraph = json.loads(Path(callgraph_path).read_text(encoding="utf-8"))
        if "graph" in callgraph:
            callgraph = callgraph["graph"]
    ir = build_icfg(root, callgraph=callgraph)
    outdir = Path(out)
    outdir.mkdir(parents=True, exist_ok=True)
    save_program_ir(ir, outdir / "program_ir.json")
    save_json({
        "target": str(root),
        "functions": len(ir.functions),
        "blocks": len(ir.blocks),
        "edges": len(ir.edges),
        "unresolved_calls": len(ir.metadata.get("unresolved_calls", [])),
        "edge_kinds": {kind: sum(e.kind == kind for e in ir.edges)
                        for kind in sorted({e.kind for e in ir.edges})},
    }, outdir / "summary.json")
    print(f"[icfg] functions={len(ir.functions)} blocks={len(ir.blocks)} edges={len(ir.edges)}")
    print(f"[out] {outdir / 'program_ir.json'}")
    return 0


def feedback_cmd(ir_path: str, trace_paths: list[str], out: str) -> int:
    ir = ProgramIR.from_json(ir_path)
    ir_stale = False                                   # P1-7:陈旧检测
    root = ir.metadata.get("root")
    if root:
        from stateforge.analysis.static.icfg import _source_hash
        try:
            ir_stale = _source_hash(Path(root)) != ir.metadata.get("source_hash")
        except OSError:
            ir_stale = True
        if ir_stale:
            print("[warn] ir is stale: sources changed since analyze",
                  file=sys.stderr)
    try:
        events = merge_events(trace_paths)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"[error] unable to read trace artifact: {exc}", file=sys.stderr)
        return 2
    report = compute_feedback(ir, events)
    payload = {
        "covered_blocks": sorted(report.overlay.covered_blocks),
        "covered_edges": [list(e) for e in sorted(report.overlay.covered_edges)],
        "dynamic_edge_kinds": {
            f"{src}\t{dst}": sorted(kinds)
            for (src, dst), kinds in sorted(report.trace.edge_kinds.items())
        },
        "frontiers": [f.__dict__ for f in report.frontiers],
        "edge_frontiers": [f.__dict__ for f in (report.edge_frontiers or [])],
        "request_count": report.request_count,
        "block_request_count": report.overlay.block_request_count,
        "block_hits": report.overlay.block_hits,
        "ir_stale": ir_stale,
        "target_distances": report.target_distances,
        "unresolved_events": len(report.trace.unresolved),
        "block_coverage": report.block_coverage,
        "coverage": coverage(ir, report.trace).to_dict(),
    }
    save_json(payload, out)
    print(f"[feedback] blocks={len(report.overlay.covered_blocks)} "
          f"edges={len(report.overlay.covered_edges)} frontiers={len(report.frontiers)}")
    print(f"[out] {out}")
    return 0


def experiment_cmd(target: str, out: str, trace_paths: list[str] | None = None) -> int:
    """Create one reproducible structural experiment artifact.

    Atheris can be run separately with ``fuzz.harness``; supplying its ordered
    trace here closes the static/dynamic feedback loop without embedding a
    second mutation engine in StateForge.
    """
    outdir = Path(out)
    analyze_cmd(target, str(outdir))
    if trace_paths:
        feedback_cmd(str(outdir / "program_ir.json"), trace_paths,
                     str(outdir / "feedback.json"))
    else:
        print("[experiment] no trace supplied; static artifact is ready for Atheris")
    return 0


def state_cmd(target: str, out: str) -> int:
    """Session 状态面:变量/访问/谓词/域 + 转移点/守卫(分列)。

    转移点(语句级)与守卫(读侧)是两类事实,分开产出;转移记录
    (边)不在静态产物中——由 state.transition.TransitionRecorder
    在 fuzz 期对转移点前后拍快照派生(见 result["transitions"]["note"])。
    """
    from stateforge.state.observer import run_state_analysis
    root = Path(target).resolve()
    result = run_state_analysis(root)
    payload = dict(result)
    payload["target"] = str(root)
    outpath = Path(out)
    outpath.parent.mkdir(parents=True, exist_ok=True)
    save_json(payload, outpath)
    transitions = result["transitions"]
    print(f"[state] variables={result['n_variables']} "
          f"predicates={result['n_predicates']} "
          f"transition_points={len(transitions['transition_points'])} "
          f"guards={len(transitions['guards'])}")
    print(f"[out] {out}")
    return 0


def _load_adapter(spec: str):
    """按 ``module:attribute`` 载入适配器;工厂函数会被调用一次。

    只接受两种形态:已是带 ``execute`` 的对象,或返回该对象的工厂。其余
    一律报错——宁可启动失败,也不要在 campaign 跑到一半才发现适配器不对。
    """
    import importlib
    module_name, _, attribute = spec.partition(":")
    if not module_name or not attribute:
        raise ValueError("adapter must be 'module:attribute'")
    module = importlib.import_module(module_name)
    candidate = getattr(module, attribute)
    if not hasattr(candidate, "execute"):
        if not callable(candidate):
            raise ValueError(f"{spec} is neither an adapter nor a factory")
        candidate = candidate()
    if not callable(getattr(candidate, "execute", None)):
        raise ValueError(f"{spec} does not provide execute(data)")
    return candidate


def _load_seed_corpus(path: str) -> list[bytes]:
    """读取初始语料:单文件即单种子,目录则取其中所有常规文件(按名排序)。"""
    root = Path(path)
    if root.is_dir():
        return [item.read_bytes() for item in sorted(root.iterdir())
                if item.is_file()]
    if root.is_file():
        return [root.read_bytes()]
    raise OSError(f"seed corpus not found: {path}")


def guided_cmd(target: str, block: str, adapter_spec: str, seeds: str,
               out: str, executions: int, max_seconds: float | None,
               random_seed: int, scheduler_name: str,
               selection_policy: str | None, energy_policy: str | None,
               max_input_size: int, max_corpus_size: int,
               fault_policy: str, allow_stale_ir: bool) -> int:
    """定向灰盒 campaign(块级目标 + 距离调度的端到端入口)。"""
    from stateforge.analysis.static.icfg import build_icfg
    from stateforge.fuzzing.guidance.campaign import (
        CampaignConfig,
        GuidedCampaign,
    )
    from stateforge.fuzzing.guidance.scheduler import EmptyCorpusError
    from stateforge.fuzzing.guidance.target import (
        BasicBlockTarget,
        GuidanceError,
    )
    try:
        ir = build_icfg(Path(target).resolve())
        if block not in ir.blocks:
            print(f"[guided] unknown basic block: {block}", file=sys.stderr)
            return 2
        initial = _load_seed_corpus(seeds)
        config = CampaignConfig(
            max_executions=executions,
            max_seconds=max_seconds,
            max_input_size=max_input_size,
            max_corpus_size=max_corpus_size,
            random_seed=random_seed,
            fault_policy=fault_policy,
            allow_stale_ir=allow_stale_ir,
            output_dir=Path(out),
            scheduler_name=scheduler_name,
            selection_policy=selection_policy,
            energy_policy=energy_policy,
        )
        campaign = GuidedCampaign(ir, BasicBlockTarget(block),
                                  _load_adapter(adapter_spec), config=config)
        result = campaign.run(initial)
    except (GuidanceError, EmptyCorpusError, ValueError, OSError,
            ImportError) as exc:
        print(f"[guided] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(f"[guided] reached={result.target_reached} "
          f"executions={result.executions} "
          f"best_distance={result.best_distance} "
          f"blocks={result.unique_covered_blocks} "
          f"edges={result.unique_valid_icfg_edges}")
    print(f"[guided] stop reason: {result.termination_reason}")
    print(f"[out] {out}")
    return 0 if result.target_reached else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="StateForge structural feedback")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="DEBUG logging on stderr (default INFO)")
    sub = parser.add_subparsers(dest="command", required=True)
    goal_run = sub.add_parser("goal-run", help="derive, plan and verify a state goal")
    goal_run.add_argument("target", help="source root to analyze")
    goal_run.add_argument("--source", required=True, help="target(data) file within source root")
    goal_run.add_argument("--block", required=True, help="target basic block id")
    goal_run.add_argument("--mode", choices=("plan", "execute", "replay", "fuzz"), default="execute")
    goal_run.add_argument("--checkpoint", help="saved checkpoint for --mode replay")
    goal_run.add_argument("--fuzz-seconds", type=float, default=2,
                          help="Atheris time budget in fuzz mode (at least 1 second)")
    goal_run.add_argument("--fuzz-runs", type=int, default=100,
                          help="maximum Atheris runs in fuzz mode")
    goal_run.add_argument("--out", default="results/goal-run")
    app_goal_run = sub.add_parser(
        "app-goal-run",
        help="same pipeline for a real Django/Flask app through its own client")
    app_goal_run.add_argument("app_root", help="application source root")
    app_goal_run.add_argument("--block", required=True, help="target basic block id")
    app_goal_run.add_argument("--harness", required=True,
                              help="harness module exposing build_client/is_authenticated")
    app_goal_run.add_argument("--framework", default="django")
    app_goal_run.add_argument("--analysis-root",
                              help="analyzed subtree inside the app (default: app root)")
    app_goal_run.add_argument("--trace-root",
                              help="traced subtree (default: analysis root)")
    app_goal_run.add_argument("--adapter",
                              help="in-process fuzz target file (required in fuzz mode)")
    app_goal_run.add_argument("--python",
                              help="application interpreter (default: $SF_APP_PYTHON)")
    app_goal_run.add_argument("--user", default="probe")
    app_goal_run.add_argument("--password", default="Passw0rd!")
    app_goal_run.add_argument("--mode",
                              choices=("plan", "execute", "replay", "fuzz"),
                              default="execute")
    app_goal_run.add_argument("--checkpoint", help="saved checkpoint for --mode replay")
    app_goal_run.add_argument("--fuzz-seconds", type=float, default=2)
    app_goal_run.add_argument("--fuzz-runs", type=int, default=50)
    app_goal_run.add_argument("--out", default="results/app-goal-run")
    analyze = sub.add_parser("analyze", help="build a stable ProgramIR/ICFG")
    analyze.add_argument("target")
    analyze.add_argument("--out", default="results/structural")
    analyze.add_argument("--callgraph")
    feedback = sub.add_parser("feedback", help="map a runtime trace and find frontiers")
    feedback.add_argument("ir")
    feedback.add_argument("trace", nargs="+", help="one or more trace artifacts")
    feedback.add_argument("--out", default="results/feedback.json")
    experiment = sub.add_parser("experiment", help="build static artifact and optional feedback")
    experiment.add_argument("target")
    experiment.add_argument("--out", default="results/experiment")
    experiment.add_argument("--trace", action="append", dest="traces",
                            help="trace artifact; repeat for multiple seeds")
    viz = sub.add_parser("viz", help="static+dynamic coverage overlay dot (P1-5)")
    viz.add_argument("ir")
    viz.add_argument("feedback")
    viz.add_argument("--out", default="results/coverage.dot")
    viz.add_argument("--session-blocks",
                     help="JSON list of block ids that touch session state")
    viz.add_argument("--dynamic-edges",
                     help="JSON list of [src, dst] pairs learned from a trace")
    viz.add_argument("--function", default=None,
                     help="limit the picture to one function")
    viz.add_argument("--around", default=None,
                     help="limit to one block id plus its neighbours")
    viz.add_argument("--only-learned", action="store_true",
                     help="draw only the blocks touched by dynamic edges")
    state = sub.add_parser("state",
                           help="session-state variables/predicates/domains "
                                "+ static transition candidates (P3-M2)")
    state.add_argument("target")
    state.add_argument("--out", default="results/state.json")
    guided = sub.add_parser("guided",
                            help="directed greybox campaign toward one basic block")
    guided.add_argument("target", help="source root to analyze")
    guided.add_argument("--block", required=True,
                        help="target basic block id (as produced by analyze)")
    guided.add_argument("--adapter", required=True,
                        help="adapter or factory as 'module:attribute'")
    guided.add_argument("--seeds", required=True,
                        help="seed file or directory of seed files")
    guided.add_argument("--out", default="results/guided")
    guided.add_argument("--executions", type=int, default=10000)
    guided.add_argument("--max-seconds", type=float, default=None)
    guided.add_argument("--random-seed", type=int, default=0)
    guided.add_argument("--scheduler", choices=("guided", "uniform"),
                        default="guided")
    guided.add_argument("--selection-policy", choices=("distance", "uniform"),
                        default=None,
                        help="override scheduler selection (default: from --scheduler)")
    guided.add_argument("--energy-policy", choices=("distance", "fixed"),
                        default=None,
                        help="override energy allocation (default: from --scheduler)")
    guided.add_argument("--max-input-size", type=int, default=4096)
    guided.add_argument("--max-corpus-size", type=int, default=1000)
    guided.add_argument("--fault-policy", choices=("continue", "stop"),
                        default="continue")
    guided.add_argument("--allow-stale-ir", action="store_true",
                        help="run even if sources changed since analysis")
    args = parser.parse_args()
    setup_logging(args.verbose)
    if args.command == "goal-run":
        from stateforge.pipeline import GoalRunner
        try:
            report = GoalRunner().run(args.target, args.block, source=args.source,
                                      mode=args.mode, out=args.out, checkpoint=args.checkpoint,
                                      fuzz_seconds=args.fuzz_seconds, fuzz_runs=args.fuzz_runs)
        except OSError as exc:
            print(f"[goal-run] unable to write artifacts: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(report.summary, ensure_ascii=False, indent=2))
        print(f"[out] {args.out}")
        if report.success:
            return 0
        return 2 if report.summary["failed_stage"] in ("validation", "analysis") else 1
    if args.command == "app-goal-run":
        from stateforge.pipeline import AppGoalRunner
        try:
            report = AppGoalRunner(python=args.python, user=args.user,
                                   password=args.password).run(
                args.app_root, args.block, framework=args.framework,
                analysis_root=args.analysis_root, harness=args.harness,
                adapter=args.adapter, trace_root=args.trace_root,
                mode=args.mode, out=args.out, checkpoint=args.checkpoint,
                fuzz_seconds=args.fuzz_seconds, fuzz_runs=args.fuzz_runs)
        except OSError as exc:
            print(f"[app-goal-run] unable to write artifacts: {exc}",
                  file=sys.stderr)
            return 2
        print(json.dumps(report.summary, ensure_ascii=False, indent=2))
        print(f"[out] {args.out}")
        if report.success:
            return 0
        return 2 if report.summary["failed_stage"] in ("validation", "analysis") else 1
    if args.command == "guided":
        return guided_cmd(args.target, args.block, args.adapter, args.seeds,
                          args.out, args.executions, args.max_seconds,
                          args.random_seed, args.scheduler,
                          args.selection_policy, args.energy_policy,
                          args.max_input_size, args.max_corpus_size,
                          args.fault_policy, args.allow_stale_ir)
    if args.command == "analyze":
        return analyze_cmd(args.target, args.out, args.callgraph)
    if args.command == "experiment":
        return experiment_cmd(args.target, args.out, args.traces)
    if args.command == "state":
        return state_cmd(args.target, args.out)
    if args.command == "viz":
        from stateforge.evaluation.viz.coverage_dot import coverage_dot
        ir_obj = ProgramIR.from_json(args.ir)
        fb = json.loads(Path(args.feedback).read_text(encoding="utf-8"))
        session_blocks = (json.loads(Path(args.session_blocks).read_text(encoding="utf-8"))
                          if args.session_blocks else ())
        learned = (json.loads(Path(args.dynamic_edges).read_text(encoding="utf-8"))
                   if args.dynamic_edges else ())
        out = coverage_dot(ir_obj, fb, args.out,
                           session_blocks=session_blocks,
                           learned_edges=[tuple(e) for e in learned],
                           function=args.function, around=args.around,
                           only_learned=args.only_learned)
        print(f"[viz] {out}")
        return 0
    return feedback_cmd(args.ir, args.trace, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
