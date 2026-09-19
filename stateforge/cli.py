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


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["e2e"])
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args()
    raise SystemExit(e2e(use_llm=not args.no_llm))
