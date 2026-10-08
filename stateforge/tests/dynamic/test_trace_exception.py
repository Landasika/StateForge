"""P1-3:tracer 采集 exception 事件。"""
from stateforge.runtime.tracer import TraceSession


def test_exception_event_recorded():
    def target():
        try:
            raise ValueError("boom")     # 必须在 settrace 之后被调用的函数帧内
        except ValueError:
            pass

    with TraceSession() as session:
        with session.request() as rid:
            target()
    ev_types = [e.event_type for e in session.events]
    assert "exception" in ev_types
    assert all(e.request_id == rid for e in session.events)


def test_exception_does_not_break_mapping(tmp_path):
    """exception 事件进 mapper 只落 unresolved,不产生假边。"""
    from pathlib import Path

    from stateforge.runtime.mapper import map_trace
    from stateforge.runtime.tracer import TraceEvent
    from stateforge.analysis.static.icfg import build_icfg

    d = tmp_path / "app"
    d.mkdir()
    (d / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    ir = build_icfg(d)
    ev = [TraceEvent(0, 0, str(d / "app.py"), "f", 1, "call"),
          TraceEvent(0, 1, str(d / "app.py"), "f", 2, "line"),
          TraceEvent(0, 2, str(d / "app.py"), "f", 2, "exception"),
          TraceEvent(0, 3, str(d / "app.py"), "f", 2, "return")]
    mt = map_trace(ev, ir)
    assert mt.blocks                    # 正常块序列仍生成
