"""OWASP Benchmark for Python 基线验证(Phase-1 结构管线 on 真实规模靶场)。

流程:build_icfg(全仓) → 启动基准应用(flask test_client) → 按路由表跨步采样
发请求(TraceSession 逐请求隔离) → map_trace/compute_feedback → 覆盖/前沿/
request 维度统计 + coverage.dot 叠图。

用法:
    python -m stateforge.eval.benchmark_baseline <BenchmarkPython 路径> \
        [--sample 60] [--out results/benchmarkpython]

克隆:gh-proxy.com/https://github.com/OWASP-Benchmark/BenchmarkPython(v0.1)。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def _iter_rules(flask_app, sample: int):
    rules = sorted(
        (r.rule, sorted(r.methods - {"HEAD", "OPTIONS"}))
        for r in flask_app.url_map.iter_rules()
        if r.rule.startswith("/benchmark/") and "<" not in r.rule
    )
    if sample and len(rules) > sample:
        stride = len(rules) / sample
        rules = [rules[int(i * stride)] for i in range(sample)]
    return rules


def run_baseline(repo: str | Path, sample: int = 60, out: str | Path = "results/benchmarkpython"):
    repo = Path(repo).resolve()
    outdir = Path(out)
    outdir.mkdir(parents=True, exist_ok=True)

    # ---- 静态:全仓 ICFG ----------------------------------------------------
    from stateforge.static.icfg import build_icfg
    t0 = time.time()
    ir = build_icfg(repo)
    static_seconds = round(time.time() - t0, 1)
    call_edges = sum(1 for e in ir.edges if e.kind == "call")
    return_edges = sum(1 for e in ir.edges if e.kind == "return")
    print(f"[static] {static_seconds}s functions={len(ir.functions)} "
          f"blocks={len(ir.blocks)} edges={len(ir.edges)} "
          f"call={call_edges} return={return_edges} "
          f"unresolved={len(ir.metadata['unresolved_calls'])}")

    # ---- 动态:启动应用并采样请求 -------------------------------------------
    import os
    cwd = os.getcwd()
    os.chdir(repo)                      # app.py 依赖 os.listdir('testcode')
    sys.path.insert(0, str(repo))
    try:
        sys.modules.pop("app", None)
        import app as benchmark_app                     # noqa: E402
        from stateforge.dynamic.tracer import TraceSession
        client = benchmark_app.app.test_client()
        rules = _iter_rules(benchmark_app.app, sample)
        records = []
        with TraceSession(root=str(repo)) as session:
            for rid, (rule, methods) in enumerate(rules):
                method = "GET" if "GET" in methods else methods[0]
                with session.request() as req_id:
                    try:
                        if method == "GET":
                            client.get(rule)
                        else:
                            client.post(rule, data={})
                    except Exception as exc:            # noqa: BLE001
                        print(f"[warn] {method} {rule}: {exc}")
                    evs = [e for e in session.events if e.request_id == req_id]
                records.append({"request_id": rid, "route": rule, "method": method,
                                "events": [e.__dict__ for e in evs]})
    finally:
        os.chdir(cwd)
        sys.path.remove(str(repo))

    trace_path = outdir / "trace.jsonl"
    trace_path.write_text("\n".join(json.dumps(r, ensure_ascii=False)
                                    for r in records), encoding="utf-8")
    print(f"[dynamic] requests={len(records)} -> {trace_path}")

    # ---- 反馈:覆盖/前沿 ----------------------------------------------------
    from stateforge.dynamic.tracer import TraceEvent
    from stateforge.feedback.engine import compute_feedback
    events = [TraceEvent(**e) for r in records for e in r["events"]]
    report = compute_feedback(ir, events)
    cov = report.trace and __import__("stateforge.dynamic.coverage",
                                      fromlist=["coverage"]).coverage(ir, report.trace)
    frontier_kinds: dict[str, int] = {}
    for f in report.frontiers:
        frontier_kinds[f.kind] = frontier_kinds.get(f.kind, 0) + 1
    payload = {
        "target": str(repo),
        "static": {"seconds": static_seconds,
                   "functions": len(ir.functions),
                   "blocks": len(ir.blocks),
                   "edges": len(ir.edges),
                   "call_edges": call_edges,
                   "return_edges": return_edges,
                   "unresolved_calls": len(ir.metadata["unresolved_calls"])},
        "dynamic": {"requests": report.request_count,
                    "covered_blocks": len(report.overlay.covered_blocks),
                    "covered_edges": len(report.overlay.covered_edges),
                    "unresolved_events": len(report.trace.unresolved)},
        "coverage": cov.to_dict() if cov else None,
        "frontiers": {"total": len(report.frontiers), "kinds": frontier_kinds},
    }
    (outdir / "baseline.json").write_text(json.dumps(payload, indent=2,
                                                     ensure_ascii=False),
                                          encoding="utf-8")
    # viz 叠图(feedback payload 形态)
    from stateforge.viz.coverage_dot import coverage_dot
    fb_payload = {"covered_blocks": sorted(report.overlay.covered_blocks),
                  "frontiers": [f.__dict__ for f in report.frontiers],
                  "block_hits": report.overlay.block_hits}
    coverage_dot(ir, fb_payload, outdir / "coverage.dot")
    print(f"[feedback] covered_blocks={payload['dynamic']['covered_blocks']} "
          f"covered_edges={payload['dynamic']['covered_edges']} "
          f"frontiers={len(report.frontiers)} "
          f"block_ratio={cov.block_ratio:.3f}" if cov else "[feedback] no coverage")
    print(f"[out] {outdir}")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="OWASP Benchmark for Python baseline")
    parser.add_argument("repo", help="path to BenchmarkPython checkout")
    parser.add_argument("--sample", type=int, default=60)
    parser.add_argument("--out", default="results/benchmarkpython")
    args = parser.parse_args()
    run_baseline(args.repo, args.sample, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
