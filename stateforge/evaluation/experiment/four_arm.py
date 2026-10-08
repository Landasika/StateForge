"""Phase-4.2-G v3:四臂实验(种子注入架构,时间预算对齐)。

A = Atheris 裸跑(连续)
B = + 结构新颖度(feedback)
C = + 前沿定向(frontier + portfolio)
D = C + LLM 种子注入(frontier + portfolio + llm)

D 臂:轮间用 llm_seed_injector 从覆盖状态找 stalled frontier → LLM →
种子写入 corpus → 下一轮 Atheris 加载变异。LLM 不执行目标。
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
from pathlib import Path

from stateforge.evaluation.experiment.epoch import run_epochs


def run_target(target_py: str, ir_path: str, name: str, out: Path,
               runs: int, epochs: int, seconds: int) -> dict:
    results = []
    for k in range(runs):
        seed = 7 + k * 100
        rd = out / f"run{k:02d}"
        print(f"  [{name}] run {k+1}/{runs}", flush=True)

        total_time = epochs * seconds

        # A:连续裸跑(吞吐参照)
        a = run_epochs(target_py, ir_path, rd / "A", total_time, 1, seed,
                        "A", mode="off", use_feedback=False)
        # B:结构新颖度
        b = run_epochs(target_py, ir_path, rd / "B", seconds, epochs, seed,
                        "B", mode="feedback", use_feedback=True)
        # C:前沿定向(portfolio)
        c = run_epochs(target_py, ir_path, rd / "C", seconds, epochs, seed,
                        "C", mode="frontier", use_feedback=True,
                        policy="portfolio")
        # D:C + LLM 种子注入(轮间产种子,Atheris 变异)
        d = run_epochs(target_py, ir_path, rd / "D", seconds, epochs, seed,
                        "D", mode="frontier", use_feedback=True,
                        policy="llm")

        results.append({
            "seed": seed,
            "A_tput": round(a["total_executions"] /
                            max(a["wall_span"], 1), 0),
            "B_blocks": b["final_blocks"], "B_edges": b["final_edges"],
            "C_blocks": c["final_blocks"], "C_edges": c["final_edges"],
            "D_blocks": d["final_blocks"], "D_edges": d["final_edges"],
        })

    s = {"target": name, "runs": runs,
         "total_time": epochs * seconds,
         "per_run": [{k: v for k, v in r.items()} for r in results]}
    for arm in ("B", "C", "D"):
        vals = [r[f"{arm}_blocks"] for r in results]
        s[f"{arm}_mean"] = round(statistics.mean(vals), 1)
        s[f"{arm}_stdev"] = round(statistics.stdev(vals), 2) \
            if len(vals) > 1 else 0
    s["D_minus_C"] = round(statistics.mean(
        [r["D_blocks"] - r["C_blocks"] for r in results]), 1)
    s["D_minus_B"] = round(statistics.mean(
        [r["D_blocks"] - r["B_blocks"] for r in results]), 1)
    return s


TARGETS = [
    ("stateforge/targets/microplus/s2_api_path.py", "S2_api_path"),
    ("stateforge/targets/microplus/s1_role_chain.py", "S1_role_chain"),
    ("stateforge/targets/microplus/hash_gates.py", "hash_gates"),
]


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="results/experiment/phase42g_v3")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--seconds", type=float, default=8)
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    all_s = {}

    # 统一 IR(microplus 目录下所有靶)
    ir_dir = out / "ir"
    subprocess.run([sys.executable, "-m", "stateforge.cli", "analyze",
                    "stateforge/targets/microplus", "--out", str(ir_dir)],
                   capture_output=True, check=True)
    ir_path = str(ir_dir / "program_ir.json")

    for tp, name in TARGETS:
        s = run_target(tp, ir_path, name, out / name,
                        args.runs, args.epochs, int(args.seconds))
        all_s[name] = s
        print(f"  → {name}: B={s['B_mean']} C={s['C_mean']} "
              f"D={s['D_mean']} (D-C={s['D_minus_C']:+.1f} "
              f"D-B={s['D_minus_B']:+.1f})", flush=True)

    (out / "all_summaries.json").write_text(
        json.dumps(all_s, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\n[out] {out / 'all_summaries.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
