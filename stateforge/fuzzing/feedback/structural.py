"""Structural novelty feedback (Phase-2, S(x)=N_B(x)+N_E(x)).

约束(顾问 Phase-2 指令):
- 新颖度相对**执行前**的全局覆盖快照计算,先算分后更新全局;
- 只统计有静态映射依据的动态证据:块 = mapper 解析成功的块;
  边 = edge_kinds 非空(存在静态边支撑)的 (src,dst) 对——
  unresolved 事件与无静态支撑的邻接对不计入;
- α=β=1,不做任何权重调参;禁距离/定向/状态。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from stateforge.runtime.mapper import MappedTrace


@dataclass
class Observation:
    """单个输入的结构反馈。"""

    blocks: set[str] = field(default_factory=set)     # B(x)
    edges: set[tuple[str, str]] = field(default_factory=set)  # E(x)
    new_blocks: set[str] = field(default_factory=set)  # N_B(x)
    new_edges: set[tuple[str, str]] = field(default_factory=set)  # N_E(x)

    @property
    def score(self) -> int:
        return len(self.new_blocks) + len(self.new_edges)

    @property
    def interesting(self) -> bool:
        return self.score > 0


class StructuralFeedback:
    """全局覆盖集合 + 新颖度评估;先算分(对旧快照),后更新。"""

    def __init__(self) -> None:
        self._global_blocks: set[str] = set()
        self._global_edges: set[tuple[str, str]] = set()

    # ---- 全局覆盖 ----
    @property
    def global_blocks(self) -> set[str]:
        return set(self._global_blocks)

    @property
    def global_edges(self) -> set[tuple[str, str]]:
        return set(self._global_edges)

    def update_global_coverage(self, blocks: set[str],
                                edges: set[tuple[str, str]]) -> None:
        """把一批观察并入全局(仅在评分完成后调用)。"""
        self._global_blocks |= blocks
        self._global_edges |= edges

    # ---- 评估 ----
    @staticmethod
    def evidence(trace: MappedTrace) -> tuple[set[str], set[tuple[str, str]]]:
        """从映射轨迹提取可靠证据:mapper 解析块 + 有静态边支撑的边对。"""
        blocks = set(trace.block_hits)
        edges = set(trace.edge_kinds)          # 只含有静态 kind 的对
        return blocks, edges

    def observe(self, trace: MappedTrace) -> Observation:
        """相对执行前全局快照计算新颖度;不修改全局。"""
        blocks, edges = self.evidence(trace)
        return Observation(
            blocks=blocks, edges=edges,
            new_blocks=blocks - self._global_blocks,
            new_edges=edges - self._global_edges,
        )
