"""Coverage counters derived from an ordered mapped trace."""

from __future__ import annotations

from dataclasses import dataclass

from stateforge.core.ir import ProgramIR
from stateforge.runtime.mapper import MappedTrace


@dataclass(frozen=True)
class Coverage:
    blocks: int
    total_blocks: int
    edges: int
    total_edges: int
    calls: int
    total_calls: int

    @property
    def block_ratio(self) -> float:
        return self.blocks / max(self.total_blocks, 1)

    @property
    def edge_ratio(self) -> float:
        return self.edges / max(self.total_edges, 1)

    @property
    def call_ratio(self) -> float:
        return self.calls / max(self.total_calls, 1)

    def to_dict(self) -> dict[str, int | float]:
        return {
            "blocks": self.blocks,
            "total_blocks": self.total_blocks,
            "block_ratio": self.block_ratio,
            "edges": self.edges,
            "total_edges": self.total_edges,
            "edge_ratio": self.edge_ratio,
            "calls": self.calls,
            "total_calls": self.total_calls,
            "call_ratio": self.call_ratio,
        }


def coverage(ir: ProgramIR, trace: MappedTrace) -> Coverage:
    edge_kinds = {"flow", "true", "false", "call", "return"}
    static_edges = {(e.src, e.dst, e.kind) for e in ir.edges if e.kind in edge_kinds}
    static_calls = {(e.src, e.dst, e.kind) for e in ir.edges if e.kind == "call"}
    covered_edges = {
        (src, dst, kind)
        for (src, dst), kinds in trace.edge_kinds.items()
        for kind in kinds
        if (src, dst, kind) in static_edges
    }
    # Traces produced before edge_kinds existed remain usable.  In that case
    # count each matching pair once rather than inventing an edge kind.
    if not trace.edge_kinds:
        covered_edges = {
            edge for edge in static_edges
            if (edge[0], edge[1]) in trace.edge_hits
        }
    dynamic_calls = covered_edges & static_calls
    return Coverage(len(trace.block_hits), len(ir.blocks),
                    len(covered_edges), len(static_edges),
                    len(dynamic_calls), len(static_calls))
