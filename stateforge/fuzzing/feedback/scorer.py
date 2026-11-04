"""Small, auditable structural seed scorer."""

from __future__ import annotations

from collections.abc import Mapping

from stateforge.core.ir import ProgramIR
from stateforge.runtime.mapper import MappedTrace
from stateforge.fuzzing.feedback.distance import distance_to_targets


def trace_distance(trace: MappedTrace, distances: Mapping[str, int]) -> int | None:
    """轨迹中已命中块到目标的最近静态距离;一个都够不着则为 None。

    None 与 0 语义必须分开:None = 静态图上无路(未知),0 = 已到目标。
    定向灰盒的调度/保留都靠这个区分,不能当成"距离很大"。
    """
    known = [distances[block] for block in trace.blocks if block in distances]
    return min(known) if known else None


def score_distance(distance: int | None) -> float:
    """把距离映射到 (0, 1] 的得分:越近越高,None 恒为 0。"""
    if distance is None:
        return 0.0
    return 1.0 / (1.0 + distance)


def score_trace_cached(trace: MappedTrace, distances: Mapping[str, int]) -> float:
    """用预算算好的距离表打分(O(命中块数),不在热路径重跑图搜索)。"""
    if not trace.blocks or not distances:
        return 0.0
    return score_distance(trace_distance(trace, distances))


def score_trace(ir: ProgramIR, trace: MappedTrace, targets: set[str],
                distances: Mapping[str, int] | None = None) -> float:
    if not targets or not trace.blocks:
        return 0.0
    if distances is None:
        distances = distance_to_targets(ir, set(trace.blocks), targets)
    return score_trace_cached(trace, distances)


def score_frontier(ir: ProgramIR, src: str, dst: str) -> float:
    distances = distance_to_targets(ir, {src}, {dst})
    return 1.0 / (1.0 + distances.get(src, 10**9))
