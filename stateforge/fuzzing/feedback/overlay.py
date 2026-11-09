"""Static/dynamic coverage overlay."""

from __future__ import annotations

from dataclasses import dataclass

from stateforge.core.ir import ProgramIR
from stateforge.runtime.mapper import MappedTrace


@dataclass
class Overlay:
    covered_blocks: set[str]
    covered_edges: set[tuple[str, str]]
    block_hits: dict[str, int]
    edge_hits: dict[tuple[str, str], int]
    # P1-4:per-request 维度(块/边被多少个不同请求命中)
    block_request_count: dict[str, int]
    edge_request_count: dict[tuple[str, str], int]


def build_overlay(ir: ProgramIR, trace: MappedTrace) -> Overlay:
    valid = {(edge.src, edge.dst) for edge in ir.edges} & set(trace.edge_hits)
    return Overlay(set(trace.block_hits), valid,
                   dict(trace.block_hits), {e: trace.edge_hits[e] for e in valid},
                   {b: len(r) for b, r in trace.block_requests.items()},
                   {e: len(trace.edge_requests.get(e, set())) for e in valid})
