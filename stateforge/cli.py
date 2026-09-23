"""StateForge structural feedback command line interface."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from stateforge.core.ir import ProgramIR
from stateforge.core.serialization import save_json, save_program_ir
from stateforge.dynamic.io import merge_events
from stateforge.dynamic.coverage import coverage
from stateforge.feedback.engine import compute_feedback
from stateforge.static.icfg import build_icfg


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
        from stateforge.static.icfg import _source_hash
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


def main() -> int:
    parser = argparse.ArgumentParser(description="StateForge structural feedback")
    sub = parser.add_subparsers(dest="command", required=True)
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
    args = parser.parse_args()
    if args.command == "analyze":
        return analyze_cmd(args.target, args.out, args.callgraph)
    if args.command == "experiment":
        return experiment_cmd(args.target, args.out, args.traces)
    if args.command == "viz":
        from stateforge.viz.coverage_dot import coverage_dot
        ir_obj = ProgramIR.from_json(args.ir)
        fb = json.loads(Path(args.feedback).read_text(encoding="utf-8"))
        out = coverage_dot(ir_obj, fb, args.out)
        print(f"[viz] {out}")
        return 0
    return feedback_cmd(args.ir, args.trace, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
