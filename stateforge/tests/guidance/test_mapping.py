"""mapper 新增的 valid/unmatched 记账:不得改变旧 ``edges`` 口径。

``edges`` 仍是"运行时相邻序列"(旧调用方依赖它);新表只在其上做静态校验。
两者混用会让 coverage 数字虚高——这是必须钉住的边界。
"""

from __future__ import annotations

from stateforge.runtime.mapper import map_trace
from stateforge.runtime.tracer import TraceEvent
from stateforge.tests.guidance.helpers import (
    build_target_ir,
    target_block_id,
)


def event(index: int, line: int, function: str = "target",
          file: str = "target.py", rid: int = 0) -> TraceEvent:
    return TraceEvent(rid, index, file, function, line)


def test_valid_and_unmatched_partition_edges() -> None:
    ir = build_target_ir()
    target = target_block_id(ir)
    entry = ir.functions["target.target"].entry
    # 构造一条真实路径:entry → data[0] 分支 → stage_one block
    branch = next(bid for bid, b in ir.blocks.items()
                  if b.kind == "branch" and "<test> data[0] > 20" in b.statements)
    block = next(bid for bid, b in ir.blocks.items()
                 if any("stage_one()" in s for s in b.statements))
    lines = [ir.blocks[entry].start_line,
             ir.blocks[branch].start_line,
             ir.blocks[block].start_line]
    trace = map_trace([event(i, line) for i, line in enumerate(lines)], ir)
    # 分区:每条相邻边要么合法要么不匹配,不重复、不遗漏
    assert set(trace.observed_valid_edges).isdisjoint(set(trace.unmatched_transitions))
    assert (set(trace.observed_valid_edges)
            | set(trace.unmatched_transitions)) == set(trace.edges)
    assert len(trace.observed_valid_edges) == len(set(trace.observed_valid_edges))
    # edges 口径未变:仍是相邻序列(含不匹配的那些)
    assert len(trace.edges) >= len(trace.observed_valid_edges)


def test_valid_edges_are_real_icfg_edges() -> None:
    ir = build_target_ir()
    target = target_block_id(ir)
    valid = {(edge.src, edge.dst) for edge in ir.edges}
    entry = ir.functions["target.target"].entry
    branch = next(bid for bid, b in ir.blocks.items()
                  if b.kind == "branch" and "<test> data[0] > 20" in b.statements)
    trace = map_trace([event(0, ir.blocks[entry].start_line),
                       event(1, ir.blocks[branch].start_line)], ir)
    for pair in trace.observed_valid_edges:
        assert pair in valid
    for pair in trace.unmatched_transitions:
        assert pair not in valid


def test_empty_trace_has_empty_accounting() -> None:
    ir = build_target_ir()
    trace = map_trace([], ir)
    assert trace.observed_valid_edges == []
    assert trace.unmatched_transitions == []
    assert trace.edges == []
