"""Map ordered runtime line events to ProgramIR blocks and edges."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from stateforge.core.ir import ProgramIR
from stateforge.runtime.tracer import TraceEvent


@dataclass
class MappedTrace:
    blocks: list[str] = field(default_factory=list)
    edges: list[tuple[str, str]] = field(default_factory=list)
    block_hits: Counter = field(default_factory=Counter)
    edge_hits: Counter = field(default_factory=Counter)
    edge_kinds: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    unresolved: list[TraceEvent] = field(default_factory=list)
    block_requests: dict[str, set[int]] = field(default_factory=dict)
    edge_requests: dict[tuple[str, str], set[int]] = field(default_factory=dict)
    observed_valid_edges: list[tuple[str, str]] = field(default_factory=list)
    unmatched_transitions: list[tuple[str, str]] = field(default_factory=list)


def map_trace(events: list[TraceEvent], ir: ProgramIR) -> MappedTrace:
    # Keep every same-name definition. A call event identifies its definition
    # line; subsequent events use that frame's block membership, not a global
    # short-name lookup. This also works with the frozen legacy trace schema.
    functions: dict[tuple[str, str], list] = {}
    for function in ir.functions.values():
        functions.setdefault((function.file, function.name.rsplit(".", 1)[-1]), []).append(function)
    by_func: dict[tuple[str, str], list[str]] = {}
    for bid, block in ir.blocks.items():
        by_func.setdefault((block.file, block.function), []).append(bid)

    def resolve(event):
        choices = functions.get((event.file, event.function), [])
        if len(choices) == 1:
            return choices[0]
        if event.event_type == "call":
            exact = [f for f in choices if ir.blocks[f.entry].start_line == event.line]
        else:
            exact = [f for f in choices if any(
                event.line in ir.blocks[b].line_numbers
                and ir.blocks[b].kind not in ("entry", "exit", "callsite", "returnsite")
                for b in f.blocks)]
        return exact[0] if len(exact) == 1 else None

    out = MappedTrace()
    previous = None
    previous_request = None
    cs_chain: dict[tuple[str, int, str], list[str]] = {}
    rs_for_cs: dict[str, str] = {}
    owner_by_block = {bid: owner for owner in ir.functions.values() for bid in owner.blocks}
    return_edges = {(e.dst, e.callee) for e in ir.edges if e.kind == "return"}
    for edge in ir.edges:
        block = ir.blocks.get(edge.src)
        if edge.kind != "call" or block is None or block.kind != "callsite":
            continue
        # CS and RS share the complete caller identity and ordinal in their
        # IDs. Never pair returns using a callee and line shared by callers.
        rs = edge.src.replace("::callsite::", "::returnsite::")
        if (rs, edge.callee) in return_edges:
            rs_for_cs[edge.src] = rs
        owner = owner_by_block.get(edge.src)
        if owner is not None:
            cs_chain.setdefault((owner.entry, edge.callsite_line or block.start_line,
                                 edge.dst), []).append(edge.src)
    # An expression can emit more line events between operand calls. Those
    # update caller location without executing its host block a second time.
    host_for_rs = {}
    incoming_flow = {}
    for edge in ir.edges:
        if edge.kind in ("flow", "true", "false"):
            incoming_flow.setdefault(edge.dst, []).append(edge.src)
    cs_for_rs = {rs: cs for cs, rs in rs_for_cs.items()}
    for rs, cs in cs_for_rs.items():
        pending, seen, hosts = [cs], set(), set()
        while pending:
            node = pending.pop()
            if node in seen:
                continue
            seen.add(node)
            for src in incoming_flow.get(node, []):
                if src in cs_for_rs:
                    pending.append(cs_for_rs[src])
                elif ir.blocks[src].kind not in ("callsite", "returnsite"):
                    hosts.add(src)
        host_for_rs[rs] = hosts
    for chain in cs_chain.values():
        chain.sort(key=lambda b: int(b.rsplit("::", 1)[-1]))

    # Each dynamic frame retains its caller line and ordinal while callees run.
    # A new line event starts a new evaluation (including another loop turn).
    frames: list[dict] = []
    valid_kinds: dict[tuple[str, str], set[str]] = {}
    for edge in ir.edges:
        valid_kinds.setdefault((edge.src, edge.dst), set()).add(edge.kind)
    valid_seen = set()
    unmatched_seen = set()

    def append_block(hit):
        nonlocal previous
        rid = previous_request if previous_request is not None else -1
        out.block_requests.setdefault(hit, set()).add(rid)
        if previous == hit:
            return
        out.blocks.append(hit)
        out.block_hits[hit] += 1
        if previous is not None:
            pair = (previous, hit)
            out.edges.append(pair)
            out.edge_hits[pair] += 1
            out.edge_requests.setdefault(pair, set()).add(rid)
            if pair in valid_kinds:
                if pair not in valid_seen:
                    valid_seen.add(pair)
                    out.observed_valid_edges.append(pair)
                out.edge_kinds.setdefault(pair, set()).update(valid_kinds[pair])
            elif pair not in unmatched_seen:
                unmatched_seen.add(pair)
                out.unmatched_transitions.append(pair)
        previous = hit

    for event in events:
        if previous_request is not None and event.request_id != previous_request:
            previous = None
            frames = []
        previous_request = event.request_id
        if event.event_type == "call":
            function = resolve(event)
            cs = None
            if function is not None and frames:
                caller = frames[-1]
                owner = caller["function"]
                if owner is not None:
                    key = (owner.entry, caller["line"], function.entry)
                    chain = cs_chain.get(key, [])
                    index = caller["hits"].get(key, 0)
                    reachable = [site for site in chain if (previous, site) in valid_kinds]
                    if len(reachable) == 1:
                        cs = reachable[0]
                        caller["hits"][key] = chain.index(cs) + 1
                    elif not reachable and index < len(chain):
                        cs = chain[index]
                        caller["hits"][key] = index + 1
                    elif len(reachable) > 1:
                        out.unresolved.append(event)
                    # Multiple reachable same-line calls to the same callee
                    # cannot be distinguished by the frozen line-only trace.
                    # Leave CS/RS unresolved rather than inventing coverage.
            if cs is not None:
                append_block(cs)
            frames.append({"function": function, "file": event.file,
                           "name": event.function, "line": None, "hits": {}, "cs": cs})
            if function is not None:
                append_block(function.entry)
            else:
                out.unresolved.append(event)
            continue

        frame = frames[-1] if frames else None
        if frame is not None and (frame["file"], frame["name"]) != (event.file, event.function):
            frame = None
        function = frame["function"] if frame else resolve(event)
        if event.event_type == "return":
            if function is not None:
                append_block(function.exit)
            else:
                out.unresolved.append(event)
            if frame is not None:
                frames.pop()
                rs = rs_for_cs.get(frame["cs"])
                if rs is not None:
                    append_block(rs)
            continue
        candidates = function.blocks if function is not None else by_func.get((event.file, event.function), [])
        # With ambiguous same-name definitions, do not map entry/exit or a
        # guessed range from another method. Exact body lines may still resolve.
        exact = [b for b in candidates if event.line in ir.blocks[b].line_numbers
                 and ir.blocks[b].kind not in ("callsite", "returnsite", "entry", "exit")]
        pool = exact or [b for b in candidates
                         if ir.blocks[b].kind not in ("callsite", "returnsite", "entry", "exit")
                         and ir.blocks[b].start_line <= event.line <= ir.blocks[b].end_line]
        if not pool and event.event_type == "line" and function is not None:
            entry = ir.blocks[function.entry]
            if event.line == entry.start_line:
                # Legacy line-only traces may encode the function entry as a
                # line at its definition instead of a separate call event.
                pool = [function.entry]
        hit = pool[0] if len(pool) == 1 else None
        if hit is None:
            out.unresolved.append(event)
            continue
        if frame is None:
            frame = {"function": function, "file": event.file, "name": event.function,
                     "line": None, "hits": {}, "cs": None}
            frames.append(frame)
        continuing_expression = (
            event.event_type == "line" and frame["line"] != event.line
            and hit in host_for_rs.get(previous, set()))
        if event.event_type == "line":
            frame["line"] = event.line
            if not continuing_expression:
                frame["hits"] = {}
        if not continuing_expression:
            append_block(hit)
    return out
