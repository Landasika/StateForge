"""Phase-3.1:DistanceOracle——反边多源 BFS,按 frontier_version 缓存。

d(v,F) = v 沿 ICFG 前向边到最近前沿锚点(Edge Frontier 的源块 u)的跳数。
全边等权(flow/true/false/call/return=1);CS/RS 参与距离图。
UNREACHABLE = 不在距离表里 → proximity=0,单独统计。
"""

from __future__ import annotations

from stateforge.core.ir import ProgramIR
from stateforge.fuzzing.feedback.distance import reverse_graph, reverse_distances


class DistanceOracle:
    def __init__(self, ir: ProgramIR):
        self._reverse = reverse_graph(ir)
        self._version: int | None = None
        self._dist: dict[str, int] = {}
        self._nearest: dict[str, str] = {}
        self.unreachable_count = 0            # 观测到的不可达 trace 块数(诊断)

    def rebuild(self, edge_frontiers: list[tuple[str, str]], version: int) -> None:
        if version == self._version:
            return
        self._version = version
        anchors = {u: f"{u}->{v}" for u, v in edge_frontiers}
        self._dist, self._nearest = reverse_distances(self._reverse, anchors)

    def distance(self, blocks: set[str]) -> tuple[int | None, str | None]:
        """D(x) = min d(v,F);None = 全部不可达(UNREACHABLE,proximity=0)。"""
        best_d, best_f = None, None
        for b in blocks:
            d = self._dist.get(b)
            if d is not None and (best_d is None or d < best_d):
                best_d, best_f = d, self._nearest[b]
        if best_d is None and blocks:
            self.unreachable_count += 1
        return best_d, best_f
