"""Tracer file-filter cache: correctness of the hot path."""
from pathlib import Path

from stateforge.runtime.tracer import TraceSession


def test_events_use_resolved_paths_and_respect_root(tmp_path: Path):
    source = tmp_path / "app.py"
    source.write_text(
        "def outer():\n"
        "    inner()\n"
        "    inner()\n"   # 第二次调用:同文件事件走缓存路径
        "def inner():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    ns: dict = {}
    exec(compile(source.read_text(), str(source), "exec"), ns)
    session = TraceSession(tmp_path)
    with session:
        ns["outer"]()
    assert session.events, "root 内文件必须有事件"
    assert all(str(e.file).startswith(str(tmp_path)) for e in session.events)
    # 同文件多事件(缓存命中路径)也要记录
    assert sum(1 for e in session.events if e.function == "inner") >= 4
    # root 外不记录:trace 期间调用标准库
    session2 = TraceSession(tmp_path)
    with session2:
        ns["outer"]()
        import json as _json
        _json.dumps({"a": 1})  # 标准库文件,不在 root
    assert not any("json" in e.file for e in session2.events)


def test_no_root_traces_everything(tmp_path: Path):
    source = tmp_path / "app.py"
    source.write_text("def f():\n    return 1\n", encoding="utf-8")
    ns: dict = {}
    exec(compile(source.read_text(), str(source), "exec"), ns)
    session = TraceSession(None)
    with session:
        ns["f"]()
    assert any(str(e.file).startswith(str(tmp_path)) for e in session.events)
