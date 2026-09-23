"""P1-7:source_hash 存在且内容变化后失效。"""
import os

from stateforge.static.icfg import _source_hash, build_icfg


def _app(tmp_path):
    d = tmp_path / "app"
    d.mkdir()
    (d / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    return d


def test_hash_present_and_changes(tmp_path):
    d = _app(tmp_path)
    ir = build_icfg(d)
    assert ir.metadata["source_hash"] == _source_hash(d)
    p = d / "app.py"
    st = p.stat()
    p.write_text("def f():\n    return 2\n", encoding="utf-8")
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    assert _source_hash(d) != ir.metadata["source_hash"]


def test_cli_feedback_marks_stale(tmp_path, capsys):
    import json
    import subprocess
    import sys

    d = _app(tmp_path)
    out = tmp_path / "exp"
    r = subprocess.run(
        [sys.executable, "-m", "stateforge.cli", "analyze", str(d),
         "--out", str(out)],
        capture_output=True, text=True, check=True)
    # 源未变:feedback 不告警
    ir_json = out / "program_ir.json"
    trace = tmp_path / "t.jsonl"
    trace.write_text('{"request_id": 0, "index": 0, "file": "%s",'
                     ' "function": "f", "line": 1, "event_type": "call"}\n'
                     % (d / "app.py"), encoding="utf-8")
    r = subprocess.run(
        [sys.executable, "-m", "stateforge.cli", "feedback", str(ir_json),
         str(trace), "--out", str(tmp_path / "fb.json")],
        capture_output=True, text=True, check=True)
    assert "stale" not in r.stderr
    # 源变化:feedback 告警 + ir_stale 标记
    p = d / "app.py"
    st = p.stat()
    p.write_text("def f():\n    return 3\n", encoding="utf-8")
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    r = subprocess.run(
        [sys.executable, "-m", "stateforge.cli", "feedback", str(ir_json),
         str(trace), "--out", str(tmp_path / "fb2.json")],
        capture_output=True, text=True, check=True)
    assert "stale" in r.stderr
    payload = json.loads((tmp_path / "fb2.json").read_text(encoding="utf-8"))
    assert payload["ir_stale"] is True
