"""Phase-2 模块 6:ExperimentRunner——A/B/C 三组,匹配种子,相同预算。

A:连续裸跑(off,总时长 = epochs × seconds)
B:按 epoch 重启,无结构反馈(off,libFuzzer 语料结转)——控制重启效应
C:按 epoch 重启 + 结构反馈筛选语料(feedback)——验证反馈机制
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from stateforge.experiment.epoch import run_group_epochs


def run_experiment(target: str, ir: str, out: Path, epochs: int,
                   seconds_per_epoch: float, seed: int) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    total = epochs * seconds_per_epoch
    report = {"target": target, "seed": seed, "epochs": epochs,
              "seconds_per_epoch": seconds_per_epoch, "groups": {}}
    # A:连续(off)——用单 epoch、总时长实现
    a_dir = out / "A"
    from stateforge.experiment.epoch import _run_driver, _summaries
    _run_driver(target, ir, a_dir / "lf_corpus", a_dir / "epochs",
                total, "off", seed, "A", 1)
    report["groups"]["A"] = {"runs": [_summaries(a_dir / "epochs", "A", 1)]}
    # B:重启无反馈
    report["groups"]["B"] = {"runs": [run_group_epochs(
        target, ir, out / "B", seconds_per_epoch, epochs, seed,
        "B", use_feedback=False, restart=True)]}
    # C:重启 + 结构反馈
    report["groups"]["C"] = {"runs": [run_group_epochs(
        target, ir, out / "C", seconds_per_epoch, epochs, seed,
        "C", use_feedback=True, restart=True)]}
    (out / "experiment.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase-2 A/B/C experiment")
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
        runs = g["runs"][0]
        if isinstance(runs, list):
            last = runs[-1]
            print(f"[{name}] epochs={len(runs)} exec={last['executions']} "
                  f"blocks={last['blocks']} edges={last['edges']} "
                  f"cands={last['candidates_saved']}")
        else:
            print(f"[{name}] exec={runs['executions']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
