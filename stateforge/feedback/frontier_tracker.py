"""Phase-3.1:FrontierTracker——动态 Edge Frontier(主)+ Node Frontier(辅)。

frontier_version 随覆盖增长单调递增;覆盖未变则不重算(热路径零成本)。
Phase-1 冻结口径不动:CS/RS 只作为距离图节点,不进覆盖分母。
"""

from __future__ import annotations

from stateforge.core.ir import ProgramIR

_EXEC_KINDS = {"flow", "true", "false", "call", "return"}


class FrontierTracker:
    def __init__(self, ir: ProgramIR):
        self.ir = ir
        self._static_edges = [(e.src, e.dst) for e in ir.edges
                              if e.kind in _EXEC_KINDS]
        self.covered_blocks: set[str] = set()
        self.covered_edges: set[tuple[str, str]] = set()
        self.version: int = 0
        self.edge_frontiers: list[tuple[str, str]] = []
        self.node_frontiers: list[str] = []
        self._created: dict[tuple[str, str], float] = {}
        self.lifecycle_events: list[dict] = []      # §16 crossing-time 原始事件

    def update(self, blocks: set[str], edges: set[tuple[str, str]]) -> bool:
        """并入新覆盖;覆盖增长时重算 frontier 并 version+=1,返回是否变化。"""
        grew = not (blocks <= self.covered_blocks and edges <= self.covered_edges)
        self.covered_blocks |= blocks
        self.covered_edges |= edges
        if grew:
            self._recompute()
            self.version += 1
        return grew

    def _recompute(self) -> None:
        import time
        now = time.time()
        prev = set(self.edge_frontiers)
        self.edge_frontiers = [
            (u, v) for (u, v) in self._static_edges
            if u in self.covered_blocks and (u, v) not in self.covered_edges]
        cur = set(self.edge_frontiers)
        for e in cur - prev:                        # 新生前沿
            self._created.setdefault(e, now)
        for e in prev - cur:                        # 消亡(被跨越或源块失效)
            t0 = self._created.pop(e, None)
            if t0 is not None and e in self.covered_edges:
                self.lifecycle_events.append(
                    {"edge": list(e), "created": round(t0, 3),
                     "crossed": round(now, 3),
                     "crossing_seconds": round(now - t0, 3)})
        self.node_frontiers = sorted({v for (_, v) in self.edge_frontiers
                                      if v not in self.covered_blocks})

    def drain_lifecycle(self) -> list[dict]:
        out, self.lifecycle_events = self.lifecycle_events, []
        return out

    def state(self) -> dict:
        return {"covered_blocks": sorted(self.covered_blocks),
                "covered_edges": [list(e) for e in sorted(self.covered_edges)],
                "version": self.version,
                "edge_frontiers": [list(e) for e in self.edge_frontiers]}
