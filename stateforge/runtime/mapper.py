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
    # A runtime pair can correspond to one or more static edge kinds.  Keep
    # the pair-based API above for callers that only need adjacency, while
    # retaining the structural kind information for coverage accounting.
    edge_kinds: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    unresolved: list[TraceEvent] = field(default_factory=list)
    # P1-4:request 维度——块/边被哪些请求命中
    block_requests: dict[str, set[int]] = field(default_factory=dict)
    edge_requests: dict[tuple[str, str], set[int]] = field(default_factory=dict)


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

    # P1-2 对齐:静态链 B→CS#i→callee.entry / callee.exit→RS#i。
    # cs_chain 以 (调用方文件, 调用方函数, 调用行, callee 入口) 为键、按链序 n 排列;
    # 运行时用最近的 line 事件定位调用行,第 k 次动态命中取链上第 k 个 CS。
    cs_chain: dict[tuple[str, str, int, str], list[str]] = {}
    rs_for_cs: dict[str, str] = {}
    for edge in ir.edges:
        src_block = ir.blocks.get(edge.src)
        if edge.kind == "call" and src_block is not None and src_block.kind == "callsite":
            key = (src_block.file, src_block.function,
                   edge.callsite_line or src_block.start_line, edge.dst)
            cs_chain.setdefault(key, []).append(edge.src)
        if edge.kind == "return" and edge.callsite_line is not None and edge.callee:
            cs = next((e.src for e in ir.edges
                       if e.kind == "call" and e.callee == edge.callee
                       and e.callsite_line == edge.callsite_line), None)
            if cs is not None:
                rs_for_cs[cs] = edge.dst

    call_stack: list[str] = []
    chain_hits: dict[tuple[str, str, int, str], int] = {}
    last_line: tuple[str, str, int] | None = None

    def append_block(hit: str) -> None:
        nonlocal previous
        rid_now = previous_request if previous_request is not None else -1
        out.block_requests.setdefault(hit, set()).add(rid_now)
        if out.blocks and out.blocks[-1] == hit:
            return
        out.blocks.append(hit)
        out.block_hits[hit] += 1
        if previous is not None:
            pair = (previous, hit)
            out.edges.append(pair)
            out.edge_hits[pair] += 1
            out.edge_requests.setdefault(pair, set()).add(rid_now)
            kinds = {edge.kind for edge in ir.edges
                     if edge.src == previous and edge.dst == hit}
            if kinds:
                out.edge_kinds.setdefault(pair, set()).update(kinds)
        previous = hit

    for event in events:
        if previous_request is not None and event.request_id != previous_request:
            previous = None
            call_stack = []
            chain_hits = {}
            last_line = None
        previous_request = event.request_id
        filename = event.file
        function = function_by_loc.get((filename, event.function))
        if event.event_type == "call" and function is not None:
            cs = None
            if last_line is not None:
                key = (last_line[0], last_line[1], last_line[2], function.entry)
                chain = cs_chain.get(key, [])
                idx = chain_hits.get(key, 0)
                if idx < len(chain):
                    chain_hits[key] = idx + 1
                    cs = chain[idx]
            if cs is not None:
                append_block(cs)
                call_stack.append(cs)
            append_block(function.entry)
            last_line = None
            continue
        if event.event_type == "return" and function is not None:
            append_block(function.exit)
            if call_stack:
                rs = rs_for_cs.get(call_stack.pop())
                if rs is not None:
                    append_block(rs)
            last_line = None
            continue
        candidates = by_func.get((filename, event.function), [])
        if not candidates:
            candidates = [bid for bid, block in ir.blocks.items()
                          if block.file == filename and block.function == event.function]
        # H2(评审§15):精确 line_numbers 优先(块 range 可能吞并嵌套块的行);
        # 合成 callsite/returnsite 与宿主语句同行时,优先真实块;range 兜底
        # 多行语句的中段行。
        exact = [bid for bid in candidates
                 if event.line in ir.blocks[bid].line_numbers]
        real = [bid for bid in exact
                if ir.blocks[bid].kind not in ("callsite", "returnsite")]
        pool = real or exact
        if len(pool) == 1:
            hit = pool[0]
        else:
            hit = next((bid for bid in pool
                        if ir.blocks[bid].start_line <= event.line <= ir.blocks[bid].end_line),
                       None)
            if hit is None:
                hit = next((bid for bid in candidates
                            if ir.blocks[bid].start_line <= event.line <= ir.blocks[bid].end_line),
                           None)
        if hit is None:
            out.unresolved.append(event)
            continue
        last_line = (filename, event.function, event.line)
        append_block(hit)
    return out
