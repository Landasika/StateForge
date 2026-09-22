"""Static/dynamic coverage overlay."""

from __future__ import annotations

from dataclasses import dataclass

from stateforge.core.ir import ProgramIR
from stateforge.dynamic.mapper import MappedTrace


@dataclass
class Overlay:
    covered_blocks: set[str]
    covered_edges: set[tuple[str, str]]
    block_hits: dict[str, int]
    edge_hits: dict[tuple[str, str], int]


def build_overlay(ir: ProgramIR, trace: MappedTrace) -> Overlay:
    return Overlay(set(trace.block_hits), set(trace.edge_hits),
                   dict(trace.block_hits), dict(trace.edge_hits))
