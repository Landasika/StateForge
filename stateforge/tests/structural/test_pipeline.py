"""Unit tests for the structural feedback contract."""

from pathlib import Path

from stateforge.core.ir import ProgramIR
from stateforge.dynamic.coverage import coverage
from stateforge.dynamic.mapper import map_trace
from stateforge.dynamic.tracer import TraceEvent
from stateforge.feedback.corpus import CorpusEntry, rank
from stateforge.static.icfg import build_icfg


def test_runtime_call_and_return_map_to_typed_edges(tmp_path: Path):
    source = tmp_path / "app.py"
    source.write_text(
        "def entry():\n"
        "    return helper()\n"
        "def helper():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    ir = build_icfg(tmp_path)
    events = [
        TraceEvent(0, 0, str(source), "entry", 1, "call"),
        TraceEvent(0, 1, str(source), "entry", 2, "line"),
        TraceEvent(0, 2, str(source), "helper", 3, "call"),
        TraceEvent(0, 3, str(source), "helper", 4, "line"),
        TraceEvent(0, 4, str(source), "helper", 4, "return"),
        TraceEvent(0, 5, str(source), "entry", 2, "return"),
    ]
    mapped = map_trace(events, ir)
    kinds = {kind for values in mapped.edge_kinds.values() for kind in values}
    assert "call" in kinds
    assert "return" in kinds
    stats = coverage(ir, mapped)
    assert stats.calls == 1
    assert stats.total_calls == 1


def test_corpus_ranking_is_deterministic():
    entries = [
        CorpusEntry("a", 0.5, 2, 1),
        CorpusEntry("b", 0.5, 3, 1),
        CorpusEntry("c", 0.5, 3, 1),
    ]
    assert [entry.seed for entry in rank(entries)] == ["c", "b", "a"]


def test_requests_do_not_create_cross_request_edges(tmp_path: Path):
    source = tmp_path / "app.py"
    source.write_text("def f():\n    return 1\n", encoding="utf-8")
    ir = build_icfg(tmp_path)
    events = [
        TraceEvent(0, 0, str(source), "f", 1, "call"),
        TraceEvent(0, 1, str(source), "f", 2, "line"),
        TraceEvent(0, 2, str(source), "f", 2, "return"),
        TraceEvent(1, 0, str(source), "f", 1, "call"),
        TraceEvent(1, 1, str(source), "f", 2, "line"),
    ]
    mapped = map_trace(events, ir)
    assert mapped.edges.count((mapped.blocks[2], mapped.blocks[3])) == 0


def test_program_ir_round_trip(tmp_path: Path):
    source = tmp_path / "app.py"
    source.write_text("def f():\n    return 1\n", encoding="utf-8")
    ir = build_icfg(tmp_path)
    path = tmp_path / "program_ir.json"
    ir.to_json(path)
    restored = ProgramIR.from_json(path)
    assert restored.to_dict() == ir.to_dict()
