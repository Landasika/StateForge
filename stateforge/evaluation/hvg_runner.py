"""v0.7b HVG Pilot:三臂实验(Isolated / Bundled / Repository Audit)。

用 vulnshop 的 Analyzer 候选生成 Hypothesis,真实 LLM 验证。
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from stateforge.core.log import get_logger
from stateforge.evaluation.hypothesis_gate import (
    Hypothesis, VerificationResult, Verdict, TriState, Mitigation,
    build_hypotheses_from_analyzer, annotate_ground_truth,
    hypothesis_to_prompt, hypothesis_to_bundled_prompt,
    repository_audit_prompt, score_hva)

logger = get_logger(__name__)


def parse_verdict(raw: str, hid: str) -> VerificationResult:
    """解析 LLM JSON 输出;失败 → UNCERTAIN。"""
    # 剥 markdown
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.M)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return VerificationResult(
            hid=hid, verdict=Verdict.UNCERTAIN,
            source_control=TriState.UNCERTAIN,
            sink_reachability=TriState.UNCERTAIN,
            security_mitigation=Mitigation.UNCERTAIN,
            rationale="PARSE_FAILED", evidence=[])

    def _verdict(v):
        try: return Verdict(v)
        except ValueError: return Verdict.UNCERTAIN
    def _tri(v):
        try: return TriState(v)
        except ValueError: return TriState.UNCERTAIN
    def _mit(v):
        try: return Mitigation(v)
        except ValueError: return Mitigation.UNCERTAIN

    return VerificationResult(
        hid=hid,
        verdict=_verdict(obj.get("verdict")),
        source_control=_tri(obj.get("source_control")),
        sink_reachability=_tri(obj.get("sink_reachability")),
        security_mitigation=_mit(obj.get("security_mitigation")),
        evidence=obj.get("evidence", []),
        rationale=obj.get("rationale", ""),
        missing_evidence=obj.get("missing_evidence", []))


def run_isolated(provider, hypotheses: list[Hypothesis],
                 app_dir: str) -> tuple[list[VerificationResult], dict]:
    """Arm C:每个 hypothesis 独立调用 LLM。"""
    results = []
    total_tokens = 0
    t0 = time.time()
    for h in hypotheses:
        prompt = hypothesis_to_prompt(h)
        system = "You are a security verification assistant. " \
                 "Respond ONLY with the required JSON."
        raw = provider.complete(system, prompt)
        vr = parse_verdict(raw, h.hid)
        vr.tokens_used = len(raw) // 4  # 估算
        results.append(vr)
    elapsed = time.time() - t0
    return results, {"time": round(elapsed, 1), "calls": len(hypotheses)}


def run_bundled(provider, hypotheses: list[Hypothesis],
                app_dir: str) -> tuple[list[VerificationResult], dict]:
    """Arm B:打包全部 hypothesis 一次给 LLM。"""
    prompt = hypothesis_to_bundled_prompt(hypotheses)
    system = "You are a security verification assistant. " \
             "Process ALL hypotheses and return a JSON array of results."
    t0 = time.time()
    raw = provider.complete(system, prompt)
    elapsed = time.time() - t0

    # 解析(期望 JSON array)
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.M)
    try:
        arr = json.loads(text)
        if isinstance(arr, dict):
            arr = arr.get("results", [arr])
    except json.JSONDecodeError:
        arr = []

    results = []
    for i, obj in enumerate(arr):
        hid = obj.get("hid", f"H{i+1:03d}")
        vr = parse_verdict(json.dumps(obj), hid)
        results.append(vr)
    return results, {"time": round(elapsed, 1), "calls": 1}


def run_pilot(n_hyp=10, out_dir="/tmp/hvg_pilot"):
    """跑 Pilot:Isolated(n 个)+ Bundled(1 次调用)。"""
    from stateforge.solving.llm.provider import Provider
    provider = Provider.from_env()
    if provider is None:
        logger.error("ERROR: Provider not configured")
        return

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # 生成 hypothesis
    app_dir = "stateforge/targets/vulnshop"
    all_hyps = build_hypotheses_from_analyzer(app_dir)
    logger.info(f"Generated {len(all_hyps)} hypotheses from vulnshop")

    # GT(vulnshop 已知漏洞)
    gt = {
        "vulnerabilities": [
            {"endpoint": "/orders/<id>/ship", "var": "status",
             "category": "workflow_bypass"},
            {"endpoint": "/orders/<id>/pay", "var": "paid_amount",
             "category": "payment_bypass"},
            {"endpoint": "/admin/users/<id>/role", "var": "role",
             "category": "privilege_escalation"},
            {"endpoint": "/orders/<id>/cancel", "var": "status",
             "category": "workflow_bypass"},
        ]
    }
    gt_file = out / "gt.json"
    gt_file.write_text(json.dumps(gt))
    annotate_ground_truth(all_hyps, gt_file)
    confirmed = sum(1 for h in all_hyps if h.gt_verdict == Verdict.CONFIRMED)
    logger.info(f"Ground truth: {confirmed} confirmed, "
          f"{len(all_hyps) - confirmed} not-confirmed")

    # 取前 n_hyp 个
    hyps = all_hyps[:n_hyp]
    logger.info(f"Pilot: {len(hyps)} hypotheses\n")

    # Arm C: Isolated
    logger.info("─── Arm C: Isolated ───")
    c_results, c_meta = run_isolated(provider, hyps, app_dir)
    c_scores = score_hva(c_results, hyps)
    logger.info(f"HVA={c_scores['hva']:.2f} HCR={c_scores['hcr']:.2f} "
          f"EGR={c_scores['egr']:.2f} time={c_meta['time']}s")
    for r in c_results:
        h = next((h for h in hyps if h.hid == r.hid), None)
        gt_v = h.gt_verdict.value if h else "?"
        mark = "✓" if r.verdict == (h.gt_verdict if h else Verdict.UNCERTAIN) else "✗"
        logger.debug(f"  {mark} {r.hid}: verdict={r.verdict.value:10s} gt={gt_v:10s} "
              f"evidence={len(r.evidence)}")

    # Arm B: Bundled
    logger.info("\n─── Arm B: Bundled ───")
    b_results, b_meta = run_bundled(provider, hyps, app_dir)
    b_scores = score_hva(b_results, hyps)
    logger.info(f"HVA={b_scores['hva']:.2f} HCR={b_scores['hcr']:.2f} "
          f"EGR={b_scores['egr']:.2f} time={b_meta['time']}s")

    # 汇总
    report = {
        "n_hypotheses": len(hyps),
        "confirmed_gt": confirmed,
        "arm_isolated": {"scores": c_scores, "meta": c_meta,
                         "results": [{"hid": r.hid, "verdict": r.verdict.value,
                                      "evidence": r.evidence} for r in c_results]},
        "arm_bundled": {"scores": b_scores, "meta": b_meta},
        "model": provider.model,
    }
    (out / "pilot_report.json").write_text(json.dumps(report, indent=1))
    logger.info(f"\n[out] {out / 'pilot_report.json'}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=10)
    parser.add_argument("--out", default="/tmp/hvg_pilot")
    args = parser.parse_args()
    run_pilot(args.n, args.out)
