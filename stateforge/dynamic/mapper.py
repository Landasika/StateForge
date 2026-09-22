"""Map ordered runtime line events to ProgramIR blocks and edges."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from stateforge.core.ir import ProgramIR
from stateforge.dynamic.tracer import TraceEvent


@dataclass
class MappedTrace:
    blocks: list[str] = field(default_factory=list)
    edges: list[tuple[str, str]] = field(default_factory=list)
    block_hits: Counter = field(default_factory=Counter)
    edge_hits: Counter = field(default_factory=Counter)
    # A runtime pair can correspond to one or more static edge kinds.  Keep
    # the pair-based API above for callers that only need adjacency, while
    # retaining the structural kind information for coverage accounting.
    edge_kinds: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    unresolved: list[TraceEvent] = field(default_factory=list)


def map_trace(events: list[TraceEvent], ir: ProgramIR) -> MappedTrace:
    by_func: dict[tuple[str, str], list[str]] = {}
    function_by_loc: dict[tuple[str, str], object] = {}
    for function in ir.functions.values():
        function_by_loc[(function.file, function.name.rsplit(".", 1)[-1])] = function
    for bid, block in ir.blocks.items():
        by_func.setdefault((block.file, block.function), []).append(bid)
    out = MappedTrace()
    previous: str | None = None
    previous_request: int | None = None

    def append_block(hit: str) -> None:
        nonlocal previous
        if out.blocks and out.blocks[-1] == hit:
            return
        out.blocks.append(hit)
        out.block_hits[hit] += 1
        if previous is not None:
            pair = (previous, hit)
            out.edges.append(pair)
            out.edge_hits[pair] += 1
            kinds = {edge.kind for edge in ir.edges
                     if edge.src == previous and edge.dst == hit}
            if kinds:
                out.edge_kinds.setdefault(pair, set()).update(kinds)
        previous = hit

    for event in events:
        if previous_request is not None and event.request_id != previous_request:
            previous = None
        previous_request = event.request_id
        filename = event.file
        function = function_by_loc.get((filename, event.function))
        if event.event_type == "call" and function is not None:
            append_block(function.entry)
            continue
        if event.event_type == "return" and function is not None:
            append_block(function.exit)
            continue
        candidates = by_func.get((filename, event.function), [])
        if not candidates:
            candidates = [bid for bid, block in ir.blocks.items()
                          if block.file == filename and block.function == event.function]
        hit = next((bid for bid in candidates
                     if ir.blocks[bid].start_line <= event.line <= ir.blocks[bid].end_line), None)
        if hit is None:
            out.unresolved.append(event)
            continue
        append_block(hit)
    return out
