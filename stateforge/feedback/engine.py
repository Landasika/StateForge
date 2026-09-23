"""One-pass structural feedback computation."""

from __future__ import annotations

from dataclasses import dataclass

from stateforge.core.ir import ProgramIR
from stateforge.dynamic.mapper import MappedTrace, map_trace
from stateforge.dynamic.tracer import TraceEvent
from stateforge.feedback.distance import distance_to_targets
from stateforge.feedback.frontier import Frontier, find_frontiers
from stateforge.feedback.overlay import Overlay, build_overlay


@dataclass
class FeedbackReport:
    overlay: Overlay
    frontiers: list[Frontier]
    target_distances: dict[str, int]
    trace: MappedTrace
    total_blocks: int
    request_count: int = 0                        # P1-4:不重复 request_id 计数

    @property
    def block_coverage(self) -> float:
        return len(self.overlay.covered_blocks) / max(self.total_blocks, 1)


def compute_feedback(ir: ProgramIR, events: list[TraceEvent],
                     targets: set[str] | None = None) -> FeedbackReport:
    mapped = map_trace(events, ir)
    overlay = build_overlay(ir, mapped)
    frontiers = find_frontiers(ir, overlay)
    target_set = targets or {f.dst for f in frontiers}
    distances = distance_to_targets(ir, overlay.covered_blocks, target_set)
    return FeedbackReport(overlay, frontiers, distances, mapped, len(ir.blocks),
                          request_count=len({e.request_id for e in events}))
