"""Phase-3.1:DistanceOracle——反边多源 BFS,按 frontier_version 缓存。

d(v,F) = v 沿 ICFG 前向边到最近前沿锚点(Edge Frontier 的源块 u)的跳数。
全边等权(flow/true/false/call/return=1);CS/RS 参与距离图。
UNREACHABLE = 不在距离表里 → proximity=0,单独统计。
"""

from __future__ import annotations

from collections import deque

from stateforge.core.ir import ProgramIR


class DistanceOracle:
    def __init__(self, ir: ProgramIR):
        self._reverse: dict[str, list[str]] = {}
        for edge in ir.edges:
            self._reverse.setdefault(edge.dst, []).append(edge.src)
        self._version: int | None = None
        self._dist: dict[str, int] = {}
        self._nearest: dict[str, str] = {}
        self.unreachable_count = 0            # 观测到的不可达 trace 块数(诊断)

    def rebuild(self, edge_frontiers: list[tuple[str, str]], version: int) -> None:
        if version == self._version:
            return
        self._version = version
        self._dist, self._nearest = {}, {}
        queue = deque()
        for (u, v) in edge_frontiers:          # 锚点:未走边的源块 u(距它 0)
            self._dist[u] = 0
            self._nearest[u] = f"{u}->{v}"
            queue.append(u)
        while queue:
            node = queue.popleft()
            d = self._dist[node]
            for parent in self._reverse.get(node, []):
                if parent not in self._dist:
                    self._dist[parent] = d + 1
                    self._nearest[parent] = self._nearest[node]
                    queue.append(parent)

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
