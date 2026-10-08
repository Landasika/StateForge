"""P1-2/P1-3:动态 call→CS→callee.entry、callee.exit→RS 的映射。"""
from pathlib import Path

from stateforge.runtime.mapper import map_trace
from stateforge.runtime.tracer import TraceEvent
from stateforge.analysis.static.icfg import build_icfg

SRC = '''\
def handler(u):
    a = first(u)
    b = second(a)
    return b

def first(x):
    return x

def second(y):
    return y
'''


def _run(tmp_path):
    d = tmp_path / "app"
    d.mkdir()
    (d / "app.py").write_text(SRC, encoding="utf-8")
    ir = build_icfg(d)
    f = str(d / "app.py")
    ev = [
        TraceEvent(0, 0, f, "handler", 2, "line"),
        TraceEvent(0, 1, f, "first", 1, "call"),
        TraceEvent(0, 2, f, "first", 2, "line"),
        TraceEvent(0, 3, f, "first", 2, "return"),
        TraceEvent(0, 4, f, "handler", 3, "line"),
        TraceEvent(0, 5, f, "second", 1, "call"),
        TraceEvent(0, 6, f, "second", 2, "line"),
        TraceEvent(0, 7, f, "second", 2, "return"),
        TraceEvent(0, 8, f, "handler", 4, "line"),
    ]
    return ir, map_trace(ev, ir)


def test_call_passes_through_callsite(tmp_path):
    ir, mt = _run(tmp_path)
    kinds = {b: ir.blocks[b].kind for b in mt.blocks}
    seq = [b for b in mt.blocks if kinds[b] in ("callsite", "returnsite")]
    # CS(first) → [first] → RS(first) → CS(second) → [second] → RS(second)
    assert [kinds[b] for b in seq] == ["callsite", "returnsite",
                                       "callsite", "returnsite"]


def test_return_edge_kinds_present(tmp_path):
    ir, mt = _run(tmp_path)
    assert [p for p, ks in mt.edge_kinds.items() if "return" in ks]
    assert [p for p, ks in mt.edge_kinds.items() if "call" in ks]


def test_call_stack_reset_per_request(tmp_path):
    d = tmp_path / "app"
    d.mkdir()
    (d / "app.py").write_text(SRC, encoding="utf-8")
    ir = build_icfg(d)
    f = str(d / "app.py")
    ev = [
        TraceEvent(0, 0, f, "handler", 2, "line"),
        TraceEvent(0, 1, f, "first", 1, "call"),
        TraceEvent(0, 2, f, "first", 2, "return"),
        TraceEvent(1, 3, f, "handler", 2, "line"),      # 新请求:栈必须已清空
        TraceEvent(1, 4, f, "first", 1, "call"),
        TraceEvent(1, 5, f, "first", 2, "return"),
    ]
    mt = map_trace(ev, ir)
    kinds = {b: ir.blocks[b].kind for b in mt.blocks}
    cs_hits = [b for b in mt.blocks if kinds[b] == "callsite"]
    assert len(cs_hits) == 2             # 两个请求各一次,无跨请求串栈
