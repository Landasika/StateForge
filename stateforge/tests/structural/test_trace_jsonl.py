"""JSONL trace writer + loader round trip."""
import json
from pathlib import Path

from stateforge.dynamic.io import load_events
from stateforge.dynamic.tracer import TraceEvent
from stateforge.fuzz.atheris_runner import TraceJsonlWriter


def test_writer_flushes_per_input_and_survives_no_close(tmp_path: Path):
    path = tmp_path / "trace.jsonl"
    writer = TraceJsonlWriter(path, fsync_every=2)
    writer.write_input(0, b"\x01", None, [TraceEvent(0, 0, "/a.py", "f", 1, "call")])
    # 不 close、直接重新打开读取(模拟进程被 _Exit:无任何清理)
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["request_id"] == 0
    assert rec["data_sha256"] and len(rec["data_sha256"]) == 64
    assert rec["data_length"] == 1
    assert rec["exception"] is None
    assert rec["events"][0]["function"] == "f"


def test_writer_records_exception_field(tmp_path: Path):
    writer = TraceJsonlWriter(tmp_path / "t.jsonl")
    writer.write_input(3, b"\xff", "ValueError: bad", [])
    rec = json.loads((tmp_path / "t.jsonl").read_text().splitlines()[0])
    assert rec["request_id"] == 3 and rec["exception"] == "ValueError: bad"


def test_load_events_reads_jsonl_in_order(tmp_path: Path):
    path = tmp_path / "trace.jsonl"
    writer = TraceJsonlWriter(path)
    writer.write_input(0, b"\x00", None, [TraceEvent(0, 0, "/a.py", "f", 1),
                                          TraceEvent(0, 1, "/a.py", "f", 2)])
    writer.write_input(1, b"\x01", None, [TraceEvent(1, 0, "/a.py", "g", 5)])
    writer.close()
    events = load_events(path)
    assert [(e.request_id, e.index, e.function) for e in events] == \
        [(0, 0, "f"), (0, 1, "f"), (1, 0, "g")]


def test_load_events_still_reads_legacy_formats(tmp_path: Path):
    # 旧格式一:裸数组
    p1 = tmp_path / "a.json"
    p1.write_text(json.dumps([{"request_id": 0, "index": 0, "file": "/a.py",
                               "function": "f", "line": 1, "event_type": "line"}]))
    assert load_events(p1)[0].function == "f"
    # 旧格式二:harness result 对象(顶层无 request_id)
    p2 = tmp_path / "b.json"
    p2.write_text(json.dumps({"data": "00", "exception": None,
                              "events": [{"request_id": 0, "index": 0, "file": "/a.py",
                                          "function": "f", "line": 1}]}))
    assert load_events(p2)[0].function == "f"
