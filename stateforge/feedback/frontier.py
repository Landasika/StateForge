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


@dataclass(frozen=True)
class EdgeFrontier:
    """H5(评审§17):边级前沿——源块已到达、但这条边从未被观察到。

    与节点前沿(dst 未覆盖)互补:dst 已覆盖也可能存在从未走过的入边
    (A→C 未走、B→C 走过、C 已覆盖),它仍是未探索控制流。
    """

    src: str
    dst: str
    kind: str


def find_frontiers(ir: ProgramIR, overlay: Overlay) -> list[Frontier]:
    return [Frontier(e.src, e.dst, e.kind)
            for e in ir.edges
            if e.kind in {"flow", "true", "false", "call", "return"}
            and e.src in overlay.covered_blocks and e.dst not in overlay.covered_blocks]


def find_edge_frontiers(ir: ProgramIR, overlay: Overlay) -> list[EdgeFrontier]:
    return [EdgeFrontier(e.src, e.dst, e.kind)
            for e in ir.edges
            if e.kind in {"flow", "true", "false", "call", "return"}
            and e.src in overlay.covered_blocks
            and (e.src, e.dst) not in overlay.covered_edges]
