"""Phase-2 模块 6(评审修订版):八组配对实验。

A_native : 连续裸跑(off)——原生吞吐参照(TracingOverhead 分母)
A_measure: 连续 + 计量(measure)——与 B/C 同开销的覆盖基线
B        : 按 epoch 重启 + 计量(measure),libFuzzer 语料结转——重启效应
C        : 按 epoch 重启 + 计量 + SF 种子重注入(feedback)——反馈净效果
D1/D2R/D2P:前沿定向家族(frontier);E = frontier + feedback + policy=state
           (state 规划器序列注入,零 LLM)——与 four_arm 的 E 同配置
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from stateforge.evaluation.experiment.epoch import run_epochs
from stateforge.evaluation.experiment.arms import RUNNER_ARMS, manifest

GROUPS = tuple(RUNNER_ARMS)


def run_experiment(target: str, ir: str, out: Path, epochs: int,
                   seconds_per_epoch: float, seed: int,
                   groups: tuple[str, ...] | None = None) -> dict:
    """跑一组配对臂;groups=None → 全 GROUPS(P4:--groups 子集透传,
    GROUPS 常量不变;子集逐名校验,未知/空 → ValueError 列有效组)。"""
    if groups is None:
        groups = GROUPS
    else:
        groups = tuple(groups)
        unknown = [g for g in groups if g not in GROUPS]
        if unknown or not groups:
            raise ValueError(f"未知组 {unknown or '空子集'};"
                             f" 有效组: {', '.join(GROUPS)}")
    if out.exists() and any(out.iterdir()):
        raise ValueError(f"new experiment requires an empty output directory: {out}")
    out.mkdir(parents=True, exist_ok=True)
    total = epochs * seconds_per_epoch
    t0 = __import__("time").monotonic()
    report = {"target": target, "ir": str(ir), "seed": seed, "epochs": epochs,
              "seconds_per_epoch": seconds_per_epoch, "groups": {}}
    selected = {label: spec for label, spec in RUNNER_ARMS.items() if label in groups}
    (out / "arm_registry.json").write_text(json.dumps(
        manifest("runner", selected), indent=2), encoding="utf-8")
    for label, spec in selected.items():
        report["groups"][label] = run_epochs(
            target, ir, out / label,
            total if spec.continuous else seconds_per_epoch,
            1 if spec.continuous else epochs, seed, label,
            t0=t0, **spec.epoch_options())
    (out / "experiment.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase-2 八组配对实验")
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
        print(f"[{name}] exec={g['total_executions']} wall={g['wall_span']}s "
              f"blocks={g['final_blocks']} edges={g['final_edges']}"
              + (f" nsg={g['novel_seed_gain']['novel_seed_gain']}"
                 if "novel_seed_gain" in g else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
