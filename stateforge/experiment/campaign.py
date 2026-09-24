"""Phase-2 正式战役:N runs 配对种子的 A/B/C + 曲线聚合 + AUC。

时间轴注记:B/C 的每 epoch t 从子进程内起算,不含轮间重启间隙;
两组重启节拍相同,彼此公平;A 连续无间隙。B/C 与 A 的绝对时间对比
含此偏差,报告中如实标注。
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

from stateforge.experiment.epoch import _run_driver, _summaries
from stateforge.experiment.runner import run_experiment


def _load_snaps(run_dir: Path, group: str, epochs: int) -> list[dict]:
    rows = []
    for e in range(1, epochs + 1):
        p = run_dir / group / "epochs" / f"{group}-e{e}-snapshots.jsonl"
        if not p.exists():
            continue
        base = (e - 1) * None  # placeholder, 由 seconds 填
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _curve_points(run_dir: Path, group: str, epochs: int, seconds: float):
    """(t_cumulative, blocks, edges) 序列;epoch 内 t 加轮次偏移。"""
    pts = []
    for e in range(1, epochs + 1):
        p = run_dir / group / "epochs" / f"{group}-e{e}-snapshots.jsonl"
        if not p.exists():
            continue
        base = (e - 1) * seconds
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                pts.append((base + r["t"], r["blocks"], r["edges"]))
    return sorted(pts)


def _auc(points, total: float, horizon: float) -> float:
    """梯形 AUC / (horizon × total) ∈ [0,1];前向填充。"""
    if not points:
        return 0.0
    area = 0.0
    last_t, last_v = 0.0, 0.0
    for t, v, _ in points:
        area += (min(t, horizon) - last_t) * last_v
        last_t, last_v = t, v
    area += max(0.0, horizon - last_t) * last_v
    return area / (horizon * max(total, 1.0))


def run_campaign(target: str, ir: str, out: Path, runs: int, epochs: int,
                 seconds: float, seed0: int, total_blocks: int) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    per_run = []
    for k in range(runs):
        seed = seed0 + k * 100
        run_dir = out / f"run{k:02d}"
        print(f"[campaign] run {k + 1}/{runs} seed={seed}", flush=True)
        run_experiment(target, ir, run_dir, epochs, seconds, seed)
        rec = {"seed": seed}
        for group in ("A", "B", "C"):
            pts = _curve_points(run_dir, group, epochs, seconds)
            horizon = epochs * seconds
            if group == "A":                 # A 无覆盖计量,只记执行数
                rec[group] = {"auc_blocks": None,
                              "final_blocks": None,
                              "executions": _summaries(run_dir / "A" / "epochs",
                                                       "A", 1)["executions"]}
            else:
                rec[group] = {
                    "auc_blocks": round(_auc(pts, total_blocks, horizon), 4),
                    "auc_edges": round(_auc([(t, e, 0) for t, _, e in pts],
                                            total_blocks * 1.2, horizon), 4),
                    "final_blocks": pts[-1][1] if pts else 0,
                    "final_edges": pts[-1][2] if pts else 0,
                    "executions": pts[-1][0] and 0 or 0,
                }
                snaps_last = json.loads(
                    (run_dir / group / "epochs" /
                     f"{group}-e{epochs}-snapshots.jsonl").read_text()
                    .strip().splitlines()[-1])
                rec[group]["executions"] = snaps_last["executions"]
        per_run.append(rec)

    def _stats(vals):
        vs = sorted(v for v in vals if v is not None)
        if not vs:
            return {}
        q1, q3 = (statistics.quantiles(vs, n=4)[0],
                  statistics.quantiles(vs, n=4)[2]) if len(vs) >= 4 else (vs[0], vs[-1])
        return {"mean": round(statistics.mean(vs), 4),
                "median": round(statistics.median(vs), 4),
                "stdev": round(statistics.stdev(vs), 4) if len(vs) > 1 else 0.0,
                "iqr": [q1, q3], "n": len(vs)}

    summary = {"runs": runs, "epochs": epochs, "seconds": seconds,
               "seed0": seed0, "total_blocks": total_blocks, "per_run": per_run,
               "aggregate": {}}
    for group in ("B", "C"):
        summary["aggregate"][group] = {
            "auc_blocks": _stats([r[group]["auc_blocks"] for r in per_run]),
            "final_blocks": _stats([r[group]["final_blocks"] for r in per_run]),
        }
    summary["aggregate"]["A"] = {
        "executions": _stats([r["A"]["executions"] for r in per_run])}
    d = summary["aggregate"]
    summary["delta_C_minus_B"] = {
        "auc_blocks_mean": round(
            statistics.mean([r["C"]["auc_blocks"] for r in per_run]) -
            statistics.mean([r["B"]["auc_blocks"] for r in per_run]), 4),
        "final_blocks_mean": round(
            statistics.mean([r["C"]["final_blocks"] for r in per_run]) -
            statistics.mean([r["B"]["final_blocks"] for r in per_run]), 3),
    }
    # 曲线 CSV:1s 桶,跨 run 均值
    csv = ["group,t,blocks_mean,blocks_p25,blocks_p75"]
    horizon = int(epochs * seconds)
    for group in ("B", "C"):
        grid = {t: [] for t in range(0, horizon + 1)}
        for k in range(runs):
            pts = _curve_points(out / f"run{k:02d}", group, epochs, seconds)
            last = 0.0
            for t in range(0, horizon + 1):
                while pts and pts[0][0] <= t:
                    last = pts.pop(0)[1]
                grid[t].append(last)
        for t in range(0, horizon + 1):
            vs = sorted(grid[t])
            if vs:
                q1, q3 = (statistics.quantiles(vs, n=4)[0],
                          statistics.quantiles(vs, n=4)[2]) if len(vs) >= 4 \
                    else (vs[0], vs[-1])
                csv.append(f"{group},{t},{statistics.mean(vs):.2f},{q1:.2f},{q3:.2f}")
    (out / "curves.csv").write_text("\n".join(csv), encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summary, indent=1,
                                                 ensure_ascii=False),
                                      encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target")
    parser.add_argument("ir")
    parser.add_argument("--out", required=True)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--seed0", type=int, default=7)
    parser.add_argument("--total-blocks", type=int, required=True)
    args = parser.parse_args()
    s = run_campaign(args.target, args.ir, Path(args.out), args.runs,
                     args.epochs, args.seconds, args.seed0, args.total_blocks)
    print(json.dumps(s["aggregate"], indent=1, ensure_ascii=False))
    print("delta C-B:", s["delta_C_minus_B"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
