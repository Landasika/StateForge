"""战役曲线图:Coverage-vs-Time(B 对照 / C 反馈,均值线 + IQR 带)。

调色板经 dataviz validate_palette 六检通过(slot1 蓝 / slot2 橙,亮模式)。
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

COLORS = {"B": "#2a78d6", "C": "#eb6834"}
LABELS = {"B": "B: restart+measure (control)", "C": "C: restart+structural feedback"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("curves_csv")
    parser.add_argument("--out", required=True)
    parser.add_argument("--total-blocks", type=int, required=True)
    args = parser.parse_args()

    rows = list(csv.DictReader(Path(args.curves_csv).open(encoding="utf-8")))
    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=150)
    last_label_y = [None]
    for group in ("B", "C"):
        pts = [(int(r["t"]), float(r["blocks_mean"]),
                float(r["blocks_p25"]), float(r["blocks_p75"]))
               for r in rows if r["group"] == group]
        if not pts:
            continue
        ts, mean, p25, p75 = zip(*pts)
        ax.fill_between(ts, p25, p75, color=COLORS[group], alpha=0.14, lw=0)
        ax.plot(ts, mean, color=COLORS[group], lw=2, label=LABELS[group],
                solid_capstyle="round")
        y_off = 0
        if last_label_y[0] is not None and abs(mean[-1] - last_label_y[0]) < 2:
            y_off = -14                      # 终值几乎相同时错开标注,防叠字
        last_label_y[0] = mean[-1]
        ax.annotate(f"{LABELS[group]}  {mean[-1]:.0f}",
                    xy=(ts[-1], mean[-1]), xytext=(6, y_off),
                    textcoords="offset points", va="center",
                    color="#333333", fontsize=9)
    ax.set_xlabel("time (s)")
    ax.set_ylabel(f"covered basic blocks (of {args.total_blocks})")
    ax.set_title("Coverage vs Time — hash-gates target, 10 paired-seed runs")
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color="#dddddd", lw=0.6)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, loc="lower right")
    fig.tight_layout()
    fig.savefig(args.out)
    print(f"[plot] {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
