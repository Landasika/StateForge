"""Frontier detection: covered source with an uncovered static successor."""

from __future__ import annotations

from dataclasses import dataclass

from stateforge.core.ir import FlowEdge, ProgramIR
from stateforge.feedback.overlay import Overlay


@dataclass(frozen=True)
class Frontier:
    src: str
    dst: str
    kind: str
    distance: int | None = None


def find_frontiers(ir: ProgramIR, overlay: Overlay) -> list[Frontier]:
    return [Frontier(e.src, e.dst, e.kind)
            for e in ir.edges
            if e.kind in {"flow", "true", "false", "call", "return"}
            and e.src in overlay.covered_blocks and e.dst not in overlay.covered_blocks]
