"""Phase-4.2-G:四臂实验(修正版——语义靶 + 对照靶 + RealWorld)。

A = Atheris 裸跑 | B = +结构新颖度 | C = +前沿定向 | D = C + LLM 后处理
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

from stateforge.experiment.epoch import run_epochs

TARGETS = {
    "small": [
        ("stateforge/targets/microplus/semantic_concat.py",
         "semantic_concat", "/tmp/ir_semantic_concat"),
        ("stateforge/targets/microplus/semantic_role.py",
         "semantic_role", "/tmp/ir_semantic_role"),
        ("stateforge/targets/microplus/semantic_url.py",
         "semantic_url", "/tmp/ir_semantic_url"),
        ("stateforge/targets/microplus/hash_gates.py",
         "hash_gates", "/tmp/ir_hash_gates"),
    ],
    "medium": [
        ("stateforge/targets/realworld/harness.py",
         "realworld", "/tmp/rw_ir"),
    ],
}


def _run_llm_solving(app_path: Path, seed: dict, workdir: Path,
                     mock_responses: list[str]) -> dict:
    """臂 D 后半段:LLM 外部求解。"""
    from stateforge.feasibility.coverage_loop import LLMCoverageLoop
    from stateforge.llm.real_provider import MockLLMProvider
    provider = MockLLMProvider(mock_responses)
    loop = LLMCoverageLoop(app_path=app_path, seed=seed,
                           provider=provider, workdir=workdir,
                           max_rounds=4, max_attempts_per_frontier=2)
    result = loop.run()
    return {"target_reached": result.target_reached,
            "crossed": sum(1 for a in result.attempts if a.frontier_crossed),
            "gain_blocks": sum(a.new_blocks for a in result.attempts),
            "attempts": len(result.attempts),
            "corpus_admitted": result.corpus_admitted}


def run_single(target_py: str, name: str, ir_dir: str, out: Path,
               runs: int, seconds: int) -> dict:
    ir_path = str(Path(ir_dir) / "program_ir.json")
    results = []
    for k in range(runs):
        seed = 7 + k * 100
        run_dir = out / f"run{k:02d}"
        print(f"  [{name}] run {k+1}/{runs}", flush=True)

        # A_native(吞吐参照)
        a = run_epochs(target_py, ir_path, run_dir / "A", seconds, 1, seed,
                        "A", mode="off", use_feedback=False)
        # A_measure(覆盖基线)
        am = run_epochs(target_py, ir_path, run_dir / "Am", seconds, 1, seed,
                         "Am", mode="measure", use_feedback=False)
        # B(结构新颖度)
        b = run_epochs(target_py, ir_path, run_dir / "B", seconds, 3, seed,
                        "B", mode="feedback", use_feedback=True)
        # C(前沿定向 D2P)
        c = run_epochs(target_py, ir_path, run_dir / "C", seconds, 3, seed,
                        "C", mode="frontier", use_feedback=True,
                        policy="portfolio")

        # D(C 同条件 + LLM 后处理)
        llm_dir = run_dir / "D_llm"
        llm_dir.mkdir(exist_ok=True)
        mock = _mock_for(name)
        # 用 Atheris 语料中最有潜力的种子做 LLM 起点
        c_corpus = run_dir / "C" / "lf_corpus"
        best_seed = _pick_seed(c_corpus, name)
        llm_res = _run_llm_solving(Path(target_py), best_seed, llm_dir, mock)

        results.append({
            "seed": seed,
            "A_tput": round(a["total_executions"] / max(a["wall_span"], 1), 0),
            "Am_blocks": am["final_blocks"], "Am_edges": am["final_edges"],
            "B_blocks": b["final_blocks"], "B_edges": b["final_edges"],
            "C_blocks": c["final_blocks"], "C_edges": c["final_edges"],
            "D_blocks": c["final_blocks"] + llm_res["gain_blocks"],
            "D_edges": c["final_edges"],
            "D_llm": llm_res,
        })

    summary = {"target": name, "runs": runs, "per_run": results}
    for arm in ("Am", "B", "C", "D"):
        vals = [r[f"{arm}_blocks"] for r in results]
        summary[f"{arm}_mean"] = round(statistics.mean(vals), 1)
        summary[f"{arm}_stdev"] = round(statistics.stdev(vals), 2) if len(vals) > 1 else 0
    summary["D_minus_C"] = round(statistics.mean(
        [r["D_blocks"] - r["C_blocks"] for r in results]), 1)
    summary["llm_crossed_total"] = sum(
        r["D_llm"].get("crossed", 0) for r in results)
    return summary


def _mock_for(name: str) -> list[str]:
    """每靶的 LLM mock 预设(证明管线,非 oracle 性能)。"""
    def prop(kind, key, val):
        return json.dumps({"status": "PROPOSED", "source_kind": kind,
                           "source_key": key, "operation": "REPLACE",
                           "proposed_value": val,
                           "rationale": "mock", "confidence": 0.9})
    if name == "semantic_concat":
        return [prop("QUERY", "s", "admin")]
    if name == "semantic_role":
        return [prop("QUERY", "username", "administrator")]
    if name == "semantic_url":
        return [prop("QUERY", "path", "/api/v1/admin/config")]
    return []          # hash_gates/realworld:LLM 无法帮助(诚实)


def _pick_seed(corpus_dir: Path, name: str) -> dict:
    """从 Atheris 语料选种子(或用默认种子)。"""
    if corpus_dir.exists():
        files = sorted(corpus_dir.glob("*"))
        if files:
            try:
                return json.loads(files[0].read_bytes())
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
    return {"query": {"s": "x", "username": "user", "path": "/",
                       "x": "1"}}


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="results/experiment/phase42g")
    parser.add_argument("--small-runs", type=int, default=3)
    parser.add_argument("--small-seconds", type=float, default=8)
    parser.add_argument("--medium-runs", type=int, default=2)
    parser.add_argument("--medium-seconds", type=float, default=10)
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    all_s = {}

    for tier, targets in TARGETS.items():
        runs = args.small_runs if tier == "small" else args.medium_runs
        secs = args.small_seconds if tier == "small" else args.medium_seconds
        for tp, name, ir in targets:
            ir_p = Path(ir)
            if not ir_p.exists():
                subprocess.run([sys.executable, "-m", "stateforge.cli",
                                "analyze", str(Path(tp).parent),
                                "--out", str(ir_p)],
                               capture_output=True, check=True)
            s = run_single(tp, name, ir, out / name, runs, int(secs))
            all_s[f"{tier}_{name}"] = s
            print(f"  → {name}: Am={s['Am_mean']} B={s['B_mean']} "
                  f"C={s['C_mean']} D={s['D_mean']} (D-C={s['D_minus_C']}) "
                  f"llm_crossed={s['llm_crossed_total']}", flush=True)

    (out / "all_summaries.json").write_text(
        json.dumps(all_s, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\n[out] {out / 'all_summaries.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
