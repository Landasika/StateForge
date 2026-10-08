"""Small, auditable structural seed scorer."""

from __future__ import annotations

from stateforge.core.ir import ProgramIR
from stateforge.runtime.mapper import MappedTrace
from stateforge.fuzzing.feedback.distance import distance_to_targets


def score_trace(ir: ProgramIR, trace: MappedTrace, targets: set[str]) -> float:
    if not targets or not trace.blocks:
        return 0.0
    distances = distance_to_targets(ir, set(trace.blocks), targets)
    if not distances:
        return 0.0
    nearest = min(distances.values())
    return 1.0 / (1.0 + nearest)


def score_frontier(ir: ProgramIR, src: str, dst: str) -> float:
    distances = distance_to_targets(ir, {src}, {dst})
    return 1.0 / (1.0 + distances.get(src, 10**9))
