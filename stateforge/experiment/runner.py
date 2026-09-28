"""Phase-2 模块 6(评审修订版):四组配对实验。

A_native : 连续裸跑(off)——原生吞吐参照(TracingOverhead 分母)
A_measure: 连续 + 计量(measure)——与 B/C 同开销的覆盖基线
B        : 按 epoch 重启 + 计量(measure),libFuzzer 语料结转——重启效应
C        : 按 epoch 重启 + 计量 + SF 种子重注入(feedback)——反馈净效果
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from stateforge.experiment.epoch import run_epochs

GROUPS = ("A_native", "A_measure", "B", "C", "D1", "D2R", "D2P")


def run_experiment(target: str, ir: str, out: Path, epochs: int,
                   seconds_per_epoch: float, seed: int) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    total = epochs * seconds_per_epoch
    t0 = __import__("time").monotonic()
    report = {"target": target, "ir": str(ir), "seed": seed, "epochs": epochs,
              "seconds_per_epoch": seconds_per_epoch, "groups": {}}
    # A_native / A_measure:连续单进程(epochs=1, 总时长)
    report["groups"]["A_native"] = run_epochs(
        target, ir, out / "A_native", total, 1, seed, "A_native",
        mode="off", use_feedback=False, t0=t0)
    report["groups"]["A_measure"] = run_epochs(
        target, ir, out / "A_measure", total, 1, seed, "A_measure",
        mode="measure", use_feedback=False, t0=t0)
    report["groups"]["B"] = run_epochs(
        target, ir, out / "B", seconds_per_epoch, epochs, seed, "B",
        mode="measure", use_feedback=False, t0=t0)
    report["groups"]["C"] = run_epochs(
        target, ir, out / "C", seconds_per_epoch, epochs, seed, "C",
        mode="feedback", use_feedback=True, t0=t0)
    # D1 = 3.1(frontier,全局冠军语义已被组合取代,行为兼容:组合挑战最近前沿)
    report["groups"]["D1"] = run_epochs(
        target, ir, out / "D1", seconds_per_epoch, epochs, seed, "D1",
        mode="frontier", use_feedback=True, t0=t0)
    # D2-R:组合冠军,普通重注入(语料联合结转)
    report["groups"]["D2R"] = run_epochs(
        target, ir, out / "D2R", seconds_per_epoch, epochs, seed, "D2R",
        mode="frontier", use_feedback=True, t0=t0, policy="reinjection")
    # D2-P:组合冠军 + focused portfolio epoch(外部预算分配)
    report["groups"]["D2P"] = run_epochs(
        target, ir, out / "D2P", seconds_per_epoch, epochs, seed, "D2P",
        mode="frontier", use_feedback=True, t0=t0, policy="portfolio")
    (out / "experiment.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase-2 四组配对实验")
    parser.add_argument("target")
    parser.add_argument("ir")
    parser.add_argument("--out", required=True)
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    report = run_experiment(args.target, args.ir, Path(args.out),
                            args.epochs, args.seconds, args.seed)
    for name, g in report["groups"].items():
        print(f"[{name}] exec={g['total_executions']} wall={g['wall_total']}s "
              f"blocks={g['final_blocks']} edges={g['final_edges']}"
              + (f" nsg={g['novel_seed_gain']['novel_seed_gain']}"
                 if "novel_seed_gain" in g else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
