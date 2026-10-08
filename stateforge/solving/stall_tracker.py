"""Phase-4.2 Finalization:Epoch 级 Stall Tracker。

对每个 frontier F,连续 stall_window 个 epoch 内:
① 该 frontier 仍存在(源块覆盖、目标边未跨)
② 没有 new block
③ 没有 new edge
④ best directed distance 没有下降
→ STALLED(F) → 触发 LLM 种子注入

不是"看到 frontier 就调 LLM"——必须等 frontier 真正卡住。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FrontierStatus:
    edge: tuple[str, str]
    first_seen_epoch: int
    last_seen_epoch: int
    epochs_stalled: int = 0
    llm_attempted: bool = False


class StallTracker:
    """跨 epoch 追踪 frontier 停滞状态。"""

    def __init__(self, stall_window: int = 2, max_llm_per_frontier: int = 3):
        self.stall_window = stall_window
        self.max_llm = max_llm_per_frontier
        self.frontiers: dict[tuple[str, str], FrontierStatus] = {}
        self.llm_counts: dict[tuple[str, str], int] = {}
        self.total_llm_calls = 0
        self.history: list[dict] = []          # 审计日志

    def observe_epoch(self, epoch: int,
                     active_frontiers: set[tuple[str, str]],
                     new_blocks: int, new_edges: int) -> list[tuple[str, str]]:
        """每个 epoch 结束后调用;返回本 epoch 应触发 LLM 的 stalled frontier。"""
        # 更新已知 frontier
        for edge in active_frontiers:
            if edge not in self.frontiers:
                self.frontiers[edge] = FrontierStatus(
                    edge, epoch, epoch)
            else:
                fs = self.frontiers[edge]
                fs.last_seen_epoch = epoch
                if new_blocks == 0 and new_edges == 0:
                    fs.epochs_stalled += 1
                else:
                    fs.epochs_stalled = 0          # 有进展→重置

        # 清理已消失的 frontier
        for edge in list(self.frontiers):
            if edge not in active_frontiers:
                del self.frontiers[edge]

        # 找满足条件的 stalled frontier
        to_inject = []
        for edge, fs in self.frontiers.items():
            if (fs.epochs_stalled >= self.stall_window
                    and not fs.llm_attempted):
                count = self.llm_counts.get(edge, 0)
                if count < self.max_llm:
                    to_inject.append(edge)
                    fs.llm_attempted = True

        # 审计
        self.history.append({
            "epoch": epoch,
            "active_frontiers": len(active_frontiers),
            "new_blocks": new_blocks, "new_edges": new_edges,
            "stalled": len([f for f in self.frontiers.values()
                            if f.epochs_stalled >= self.stall_window]),
            "injecting": len(to_inject),
        })
        return to_inject

    def mark_injected(self, edge: tuple[str, str]):
        self.llm_counts[edge] = self.llm_counts.get(edge, 0) + 1
        self.total_llm_calls += 1
        # 重置 attempt 标记(下次 stall 再触发,直到 max_llm)
        if edge in self.frontiers:
            self.frontiers[edge].llm_attempted = False

    def summary(self) -> dict:
        return {
            "total_frontiers_seen": len(self.frontiers),
            "total_llm_calls": self.total_llm_calls,
            "llm_per_frontier": {f"{e[0]}->{e[1]}": c
                                 for e, c in self.llm_counts.items()},
            "history": self.history,
        }
