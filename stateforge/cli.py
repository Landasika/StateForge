"""cli.py —— StateForge 入口。今晚:e2e(第一层垂直切片验收)。

  python3 -m stateforge.cli e2e [--no-llm]

流水线:analyze(vulnshop+vampi) → label(规则|LLM) → select → SSTG →
ANOTA 注解起草 → 对照手写 app_anota → results/e2e-first-layer.{json,md}
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from stateforge.analyzer.analyzer import analyze
from stateforge.analyzer.selector import select
from stateforge.core.model import AppModel
from stateforge.graph.builder import build_sstg
from stateforge.graph.export import to_dot, to_json
from stateforge.llm.annotate import compare_with_handwritten, draft_annotations
from stateforge.llm.heuristic import label_rule_based
from stateforge.llm.lifting import label, relate
from stateforge.llm.provider import Provider

ROOT = Path(__file__).parent.parent
RESULTS = ROOT / "results"


def e2e(use_llm: bool = True) -> int:
    RESULTS.mkdir(exist_ok=True)
    provider = Provider.from_env() if use_llm else None
    report: dict = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "llm": bool(provider), "targets": {}}

    if provider:
        print(f"[llm] provider={provider.kind} model={provider.model} base={provider.base_url}")
    else:
        print("[llm] 无 provider → 规则标注列(链路照跑)")

    for name, root, fw in [
        ("vulnshop", ROOT / "stateforge" / "targets" / "vulnshop", "flask"),
        ("vampi", ROOT / "stateforge" / "targets" / "vampi", "connexion"),
    ]:
        t0 = time.time()
        model: AppModel = analyze(root, fw)
        domains = _domains(model)

        svs_h = select(label_rule_based(model.candidates, domains))  # heuristic 列
        llm_lift = None
        if provider:
            svs_l, lift = label(model.candidates, domains, provider=provider)
            model.state_vars = select(svs_l)
            llm_lift = relate([sv for sv in model.state_vars if sv.security_relevant],
                              model.endpoints, provider=provider)
        else:
            model.state_vars = svs_h
        from stateforge.analyzer.analyzer import derive_invariants
        model.invariants = derive_invariants(model)

        # SSTG
        sstg = build_sstg(model)
        to_json(sstg, RESULTS / f"{name}.stategraph.json")
        to_dot(sstg, RESULTS / f"{name}.stategraph.dot")

        # G_xfg v2(基于调用图添加过程内 CFG;无 fixture 自动退化为端点级)
        from stateforge.analyzer.cfg import build_xfg, xfg_to_dot
        cg_fixture = Path(root) / "golden" / "callgraph.json"
        xfg = build_xfg(model, callgraph_path=cg_fixture if cg_fixture.exists() else None)
        xfg_to_dot(xfg, RESULTS / f"{name}.xfg.dot")

        # ANOTA 注解起草
        drafts = draft_annotations(model)
        cmp_result = None
        if name == "vulnshop":
            hand = (root / "app_anota.py").read_text(encoding="utf-8")
            cmp_result = compare_with_handwritten(drafts, hand)

        sec = [sv for sv in model.state_vars if sv.security_relevant]
        report["targets"][name] = {
            "endpoints": len(model.endpoints),
            "candidates": len(model.candidates),
            "state_vars": len(model.state_vars),
            "security_relevant": {sv.var: sv.label for sv in sec},
            "invariants": {i.var: {"legal": i.legal_transitions, "one_way": i.one_way}
                           for i in model.invariants},
            "sstg": {"nodes": sstg.number_of_nodes(), "edges": sstg.number_of_edges()},
            "xfg": {"nodes": xfg.number_of_nodes(), "edges": xfg.number_of_edges(),
                    "call_edges": sum(1 for _, _, d in xfg.edges(data=True)
                                      if d.get("kind") == "call")},
            "label_source": (llm_lift.source if llm_lift else "heuristic"),
            "relate": (llm_lift.model_dump(exclude={"label_log"}) if llm_lift else None),
            "annotation_drafts": len(drafts),
            "annotation_compare": cmp_result,
            "elapsed_s": round(time.time() - t0, 2),
        }
        print(f"[{name}] endpoints={len(model.endpoints)} sec_vars={len(sec)} "
              f"labels={report['targets'][name]['label_source']} drafts={len(drafts)}")

    model.to_json(RESULTS / "vulnshop.AppModel.json")
    (RESULTS / "e2e-first-layer.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[done] → {RESULTS}/e2e-first-layer.json")
    return 0


def _domains(model: AppModel) -> dict[str, list[str]]:
    from collections import defaultdict

    values: dict[str, list[str]] = defaultdict(list)
    for ep in model.endpoints:
        for pre in ep.preconditions:
            if pre.value and pre.var != "Session.authed_as" and pre.value not in values[pre.var]:
                values[pre.var].append(pre.value)
        for eff in ep.effects:
            if eff.value and eff.var != "Session.authed_as" and eff.value not in values[eff.var]:
                values[eff.var].append(eff.value)
    return dict(values)


def analyze_cmd(target: str, framework: str, name: str | None, out: str) -> int:
    """对任意真实项目跑静态分析:AppModel + SSTG + XFG(+自动 PyCG 调用图)。"""
    from stateforge.analyzer.cfg import build_xfg, xfg_to_dot
    from stateforge.analyzer.pycg_bridge import build_callgraph
    from stateforge.analyzer.svg import render_function_xfg

    outdir = Path(out)
    outdir.mkdir(parents=True, exist_ok=True)
    tag = name or Path(target).name

    # 调用图:pycg_bridge(入口探测→隔离执行→合并→缓存),缺口如实报告
    cg, gaps = build_callgraph(target)
    cg_path = outdir / f"{tag}.callgraph.json"
    cg_path.write_text(json.dumps(cg))
    if cg:
        print(f"[callgraph] {len(cg)} nodes" + (f"; gaps: {gaps}" if gaps else ""))

    model = analyze(Path(target), framework)
    model.to_json(outdir / f"{tag}.AppModel.json")
    sstg = build_sstg(model)
    to_json(sstg, outdir / f"{tag}.stategraph.json")
    to_dot(sstg, outdir / f"{tag}.stategraph.dot")
    xfg = build_xfg(model, callgraph_path=cg_path if cg_path.exists() else None)
    xfg_to_dot(xfg, outdir / f"{tag}.xfg.dot")
    render_function_xfg(xfg, outdir / f"{tag}.xfg.svg", f"{tag} G_xfg (function-level)")

    sec = [sv for sv in model.state_vars if sv.security_relevant]
    print(f"[{tag}] endpoints={len(model.endpoints)} candidates={len(model.candidates)} "
          f"state_vars={len(model.state_vars)} sec={len(sec)} "
          f"sstg={sstg.number_of_nodes()}n xfg={xfg.number_of_nodes()}n")
    for sv in sec[:20]:
        print(f"   SEC {sv.var:32s} {sv.label}")
    print(f"[out] {outdir}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["e2e", "analyze"])
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("target", nargs="?", help="analyze:项目目录")
    ap.add_argument("--framework", default="flask", choices=["flask", "connexion"])
    ap.add_argument("--name")
    ap.add_argument("--out", default="results/analyze")
    args = ap.parse_args()
    if args.command == "e2e":
        raise SystemExit(e2e(use_llm=not args.no_llm))
    raise SystemExit(analyze_cmd(args.target, args.framework, args.name, args.out))
