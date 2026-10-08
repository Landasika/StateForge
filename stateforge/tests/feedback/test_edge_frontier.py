"""H5(评审§17):边级前沿——dst 已覆盖但边从未走过也要报。"""
from stateforge.core.ir import ProgramIR, BasicBlock, FunctionIR, FlowEdge
from stateforge.fuzzing.feedback.engine import compute_feedback
from stateforge.fuzzing.feedback.frontier import find_edge_frontiers
from stateforge.fuzzing.feedback.overlay import Overlay
from stateforge.runtime.tracer import TraceEvent


def _ir():
    ir = ProgramIR()
    f = "/t/app.py"
    for bid, lo, hi in [("a", 1, 2), ("b", 3, 4), ("c", 5, 6)]:
        ir.blocks[bid] = BasicBlock(bid, f, "app", "h", lo, hi, "block", [],
                                    list(range(lo, hi + 1)))
    ir.functions["app.h"] = FunctionIR("app.h", f, "app", "a", "c",
                                       ["a", "b", "c"])
    ir.edges.append(FlowEdge("a", "c", "flow"))
    ir.edges.append(FlowEdge("b", "c", "flow"))
    return ir


def test_unobserved_edge_reported_even_if_dst_covered():
    ir = _ir()
    ov = Overlay({"a", "b", "c"}, {("b", "c")},
                 {"a": 1, "b": 1, "c": 1}, {("b", "c"): 1},
                 {"a": 1, "b": 1, "c": 1}, {("b", "c"): 1})
    ef = find_edge_frontiers(ir, ov)
    assert ("a", "c") in {(e.src, e.dst) for e in ef}
    assert ("b", "c") not in {(e.src, e.dst) for e in ef}


def test_engine_carries_edge_frontiers():
    ir = _ir()
    ev = [TraceEvent(0, i, "/t/app.py", "h", line, "line")
          for i, line in enumerate([1, 3, 5])]        # a,b,c 都覆盖,但只走过 b→c
    report = compute_feedback(ir, ev)
    assert any(e.src == "a" for e in report.edge_frontiers)
