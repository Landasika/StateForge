"""P1-4:块/边按请求维度的计数。"""
from stateforge.core.ir import ProgramIR, BasicBlock, FunctionIR, FlowEdge
from stateforge.dynamic.mapper import map_trace
from stateforge.dynamic.tracer import TraceEvent
from stateforge.feedback.engine import compute_feedback
from stateforge.feedback.overlay import build_overlay


def _ir():
    ir = ProgramIR()
    f = "/t/app.py"
    ir.blocks["b1"] = BasicBlock("b1", f, "app", "h", 1, 2, "block", [], [1, 2])
    ir.blocks["b2"] = BasicBlock("b2", f, "app", "h", 3, 4, "block", [], [3, 4])
    ir.functions["app.h"] = FunctionIR("app.h", f, "app", "b1", "b2", ["b1", "b2"])
    ir.edges.append(FlowEdge("b1", "b2", "flow"))
    return ir


def _ev(rid, line):
    return TraceEvent(rid, rid * 10 + line, "/t/app.py", "h", line, "line")


def test_block_request_count_two_requests():
    ir = _ir()
    events = [_ev(0, 1), _ev(0, 3), _ev(1, 1)]        # 请求 0 走 b1→b2,请求 1 只走 b1
    mt = map_trace(events, ir)
    assert mt.block_requests["b1"] == {0, 1}
    assert mt.block_requests["b2"] == {0}
    ov = build_overlay(ir, mt)
    assert ov.block_request_count["b1"] == 2
    assert ov.block_request_count["b2"] == 1
    assert ov.edge_request_count[("b1", "b2")] == 1
    report = compute_feedback(ir, events)
    assert report.request_count == 2
