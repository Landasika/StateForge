"""Deviation analysis: 本次执行"该走哪个分支而没走"。

对每次执行里经过的每个分支块,比较"实际走的后继"与"静态上更靠近目标的
那个后继"。只有当实际分支明显更远时才记为一个偏差点——这不是"输入错了"
的断言,只是给出下一步该翻哪个谓词的**排序线索**;置信度随距离差增大、
随映射失败而降低。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from stateforge.core.ir import ProgramIR
from stateforge.fuzzing.feedback.distance import target_distances
from stateforge.runtime.mapper import MappedTrace


@dataclass(frozen=True)
class DeviationPoint:
    block_id: str
    observed_successor: str
    desired_successor: str
    observed_distance: int | None
    desired_distance: int
    confidence: float


class DeviationAnalyzer:
    def __init__(self, ir: ProgramIR, target_id: str,
                 distances: Mapping[str, int] | None = None):
        if target_id not in ir.blocks:
            raise ValueError(f"target basic block not found in ProgramIR: {target_id}")
        self.ir = ir
        self.target_id = target_id
        self.distances = (dict(distances) if distances is not None
                          else target_distances(ir, {target_id}))
        self._branch_successors = self._collect_branch_successors()

    def _collect_branch_successors(self) -> dict[str, tuple[str, ...]]:
        successors: dict[str, tuple[str, ...]] = {}
        for block_id in self.ir.blocks:
            targets = []
            seen = set()
            for edge in self.ir.outgoing(block_id):
                if edge.kind not in ("true", "false"):
                    continue
                if edge.dst in seen:
                    continue
                seen.add(edge.dst)
                targets.append(edge.dst)
            if targets:
                successors[block_id] = tuple(targets)
        return successors

    def analyze(self, trace: MappedTrace) -> list[DeviationPoint]:
        points: dict[tuple[str, str, str], DeviationPoint] = {}
        for pair in trace.edges:
            block_id, observed = pair
            successors = self._branch_successors.get(block_id)
            if not successors or observed not in successors:
                continue
            # 只有"确实是分支边"才谈得上走错;普通 flow 相邻不算偏差。
            kinds = trace.edge_kinds.get(pair, set())
            branch_kinds = kinds & {"true", "false"}
            if len(branch_kinds) != 1:
                continue
            finite = [(successor, self.distances[successor])
                      for successor in successors
                      if successor in self.distances]
            if not finite:
                continue
            desired_distance = min(distance for _, distance in finite)
            desired_candidates = [successor for successor, distance in finite
                                  if distance == desired_distance]
            # 两个后继一样近时不存在"更好的选择",不编造偏差。
            if len(desired_candidates) != 1:
                continue
            desired = desired_candidates[0]
            observed_distance = self.distances.get(observed)
            if observed_distance is not None and observed_distance <= desired_distance:
                continue
            key = (block_id, observed, desired)
            confidence = self._confidence(observed_distance, desired_distance,
                                          trace)
            points[key] = DeviationPoint(
                block_id=block_id,
                observed_successor=observed,
                desired_successor=desired,
                observed_distance=observed_distance,
                desired_distance=desired_distance,
                confidence=confidence,
            )
        ordered = sorted(points.values(), key=self._relevance)
        return ordered

    @staticmethod
    def _confidence(observed_distance: int | None,
                    desired_distance: int,
                    trace: MappedTrace) -> float:
        if observed_distance is None:
            value = 0.8
        else:
            gap = observed_distance - desired_distance
            value = min(0.8, 0.4 + 0.1 * gap)
        if trace.unresolved:
            value *= 0.9
        return round(value, 6)

    @staticmethod
    def _relevance(point: DeviationPoint) -> tuple:
        observed = (point.observed_distance
                    if point.observed_distance is not None else 10**9)
        return (-point.confidence, point.desired_distance, observed,
                point.block_id, point.observed_successor,
                point.desired_successor)


def find_deviation_points(ir: ProgramIR, target_id: str, trace: MappedTrace,
                          distances: Mapping[str, int] | None = None
                          ) -> list[DeviationPoint]:
    return DeviationAnalyzer(ir, target_id, distances).analyze(trace)
