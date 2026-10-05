"""HVG-P2:Source-Grounded Hypothesis Validation 上下文构建 + 三臂 runner。

三个臂(A0/A1/A2),唯一变量 = 给 LLM 的 evidence 量。
"""

from __future__ import annotations

import ast
import json
import re
import time
from pathlib import Path

from stateforge.eval.hypothesis_gate import (
    Hypothesis, VerificationResult, Verdict, TriState, Mitigation,
    build_hypotheses_from_analyzer, annotate_ground_truth,
    score_hva)


# ── 源码上下文构建 ──

def extract_source_context(app_path: Path, function_name: str,
                           max_loc: int = 150) -> str:
    """从源文件提取函数体(含函数名和行号),≤ max_loc 行。"""
    try:
        source = app_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, SyntaxError):
        return "(source unavailable)"

    lines = source.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == function_name:
            start = max(0, node.lineno - 1)
            end = min(len(lines), node.end_lineno)
            code = "\n".join(lines[start:end])
            if len(code.splitlines()) > max_loc:
                code = "\n".join(code.splitlines()[:max_loc]) + "\n... (truncated)"
            return f"```python\n# {app_path.name}:{node.lineno}-{node.end_lineno}\n{code}\n```"
    return "(function not found)"


def extract_guard_evidence(app_path: Path, function_name: str) -> str:
    """从函数 AST 提取所有分支守卫(观察,不含结论)。"""
    try:
        source = app_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, SyntaxError):
        return "(guard analysis unavailable)"

    fn = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == function_name:
            fn = node
            break
    if fn is None:
        return "(function not found for guard analysis)"

    guards = []
    for node in ast.walk(fn):
        if isinstance(node, (ast.If, ast.While)):
            test = getattr(node, "test", None)
            if test is not None:
                expr = ast.unparse(test)
                guards.append(
                    f"  - {app_path.name}:{node.lineno} `{expr}`")

    if not guards:
        return "(no branch guards observed in this function)"
    return "\n".join(guards)


# ── 三种 prompt ──

def prompt_a0(h: Hypothesis) -> str:
    """A0:Summary only(R1 基线,与 hypothesis_gate 相同)。"""
    guards_text = "\n".join(f"  - {g}" for g in h.known_guards) or "  (none)"
    return f"""Verify the following security hypothesis.

## Hypothesis {h.hid}

**Candidate:** {h.candidate}
**Source:** {h.source}
**Sink:** {h.sink}
**Known flow:** {h.known_flow}
**Known guards:**
{guards_text}
**Endpoint:** {h.endpoint}

## Output (JSON only)
{{"verdict":"CONFIRMED|REJECTED|UNCERTAIN","source_control":"CONFIRMED|REJECTED|UNCERTAIN","sink_reachability":"CONFIRMED|REJECTED|UNCERTAIN","security_mitigation":"PRESENT|ABSENT|UNCERTAIN","evidence":["file:line desc"],"rationale":"..."}}"""


def prompt_a1(h: Hypothesis, app_path: Path) -> str:
    """A1:Summary + Source(相关函数源码)。"""
    func_name = h.source.split(":")[-1] if ":" in h.source else h.source
    src = extract_source_context(app_path, func_name)
    base = prompt_a0(h)
    return base + f"""

## Relevant Source Context

{src}

Verify ONLY this hypothesis. Use the source code above as primary evidence."""


def prompt_a2(h: Hypothesis, app_path: Path) -> str:
    """A2:Summary + Source + Guard Evidence(守卫恢复观测)。"""
    func_name = h.source.split(":")[-1] if ":" in h.source else h.source
    src = extract_source_context(app_path, func_name)
    guards = extract_guard_evidence(app_path, func_name)
    base = prompt_a0(h)
    return base + f"""

## Relevant Source Context

{src}

## Observed Branch Guards (from static analysis)

{guards}

Note: These are observed predicates at branch points. Compare them with the
expected workflow and determine if any necessary check is absent.
Verify ONLY this hypothesis."""


# ── 三臂 runner ──

def run_arm(provider, hypotheses: list[Hypothesis], app_path: Path,
            arm: str) -> tuple[list[VerificationResult], dict]:
    """跑一个臂;arm = 'A0' | 'A1' | 'A2'。"""
    prompt_fn = {"A0": prompt_a0, "A1": prompt_a1, "A2": prompt_a2}[arm]
    results = []
    t0 = time.time()

    for h in hypotheses:
        if arm == "A0":
            prompt = prompt_fn(h)
        else:
            prompt = prompt_fn(h, app_path)

        system = "You are a security verification assistant. " \
                 "Respond ONLY with the required JSON."
        raw = provider.complete(system, prompt)
        vr = _parse(raw, h.hid)
        vr.tokens_used = len(raw) // 4
        results.append(vr)

    elapsed = round(time.time() - t0, 1)
    return results, {"time": elapsed, "calls": len(hypotheses), "arm": arm}


def _parse(raw: str, hid: str) -> VerificationResult:
    """解析 LLM JSON 输出。"""
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

    def _v(v, cls):
        try: return cls(v)
        except ValueError: return cls.UNCERTAIN

    return VerificationResult(
        hid=hid,
        verdict=_v(obj.get("verdict"), Verdict),
        source_control=_v(obj.get("source_control"), TriState),
        sink_reachability=_v(obj.get("sink_reachability"), TriState),
        security_mitigation=_v(obj.get("security_mitigation"), Mitigation),
        evidence=obj.get("evidence", []),
        rationale=obj.get("rationale", ""))


def score_with_ecr_esr(results: list[VerificationResult],
                        hypotheses: list[Hypothesis]) -> dict:
    """HVA + HCR + ECR(citation rate)+ ESR(需人工标注 support)。"""
    gt_map = {h.hid: h.gt_verdict for h in hypotheses}
    correct = completed = cited = total = 0
    for r in results:
        h_gt = gt_map.get(r.hid)
        if h_gt is None: continue
        total += 1
        if r.verdict != Verdict.UNCERTAIN:
            completed += 1
            if r.verdict == h_gt:
                correct += 1
        if r.evidence and any(":" in e for e in r.evidence):
            cited += 1
    return {
        "hva": round(correct / max(completed, 1), 4),
        "hcr": round(completed / max(total, 1), 4),
        "ecr": round(cited / max(total, 1), 4),
        # esr 需人工标注: 引用的证据是否真正支持 verdict
        "esr": None,
        "total": total, "completed": completed,
        "correct": correct, "cited": cited,
    }


def run_p2(n_hyp=10, out_dir="/tmp/hvg_p2"):
    """跑三个臂(A0/A1/A2),同一个 H 集合 + GT + 模型。"""
    from stateforge.llm.provider import Provider
    provider = Provider.from_env()
    if provider is None:
        print("ERROR: Provider not configured")
        return

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    app_dir = Path("stateforge/targets/vulnshop")
    app_path = app_dir / "app.py"

    # 生成同一组 hypothesis
    all_hyps = build_hypotheses_from_analyzer(app_dir)
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
    hyps = all_hyps[:n_hyp]
    confirmed = sum(1 for h in hyps if h.gt_verdict == Verdict.CONFIRMED)
    print(f"Hypotheses: {len(hyps)} ({confirmed} confirmed GT)")
    print(f"Model: {provider.model}\n")

    report = {"n": len(hyps), "model": provider.model, "arms": {}}

    for arm in ("A0", "A1", "A2"):
        print(f"─── Arm {arm} ───")
        results, meta = run_arm(provider, hyps, app_path, arm)
        scores = score_with_ecr_esr(results, hyps)
        print(f"  HVA={scores['hva']:.2f} HCR={scores['hcr']:.2f} "
              f"ECR={scores['ecr']:.2f} time={meta['time']}s")
        for r in results:
            h = next((h for h in hyps if h.hid == r.hid), None)
            gt_v = h.gt_verdict.value if h else "?"
            mark = "✓" if r.verdict == (h.gt_verdict if h else Verdict.UNCERTAIN) else "✗"
            print(f"  {mark} {r.hid}: {r.verdict.value:10s} gt={gt_v:10s} ev={len(r.evidence)}")
        report["arms"][arm] = {
            "scores": scores, "meta": meta,
            "results": [{"hid": r.hid, "verdict": r.verdict.value,
                         "evidence": r.evidence} for r in results]}
        print()

    (out / "p2_report.json").write_text(json.dumps(report, indent=1))
    print(f"[out] {out / 'p2_report.json'}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=10)
    parser.add_argument("--out", default="/tmp/hvg_p2")
    args = parser.parse_args()
    run_p2(args.n, args.out)
