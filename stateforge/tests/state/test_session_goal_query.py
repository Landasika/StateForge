"""SS-02: goal-query contract (STATIC_MAY, alternatives, determinism)."""

from __future__ import annotations

from pathlib import Path

from stateforge.analysis.static.icfg import build_icfg
from stateforge.state.session_access import KEY_UNKNOWN, extract_session_accesses
from stateforge.state.session_index import (build_session_index,
                                            query_session_frontier)

GATE = Path(__file__).resolve().parents[2] / "targets" / "session_gate"
TARGET = "app.py::protected::173-173::block::1"


def _block_at(ir, function, line):
    hits = [b for b in ir.blocks.values()
            if b.function == function and b.start_line <= line <= b.end_line]
    exact = [b for b in hits if b.start_line == line]
    return (exact or hits)[0].id


def test_ambiguous_branch_alias_is_unknown_not_first_key(tmp_path):
    (tmp_path / "app.py").write_text('''
from flask import session

def protected():
    if session.get("m") == "a":
        uid = session["uid"]
    else:
        uid = session["token"]
    if uid is None:
        return "deny"
    return "ok"
''', encoding="utf-8")
    ir = build_icfg(tmp_path)
    accesses = extract_session_accesses(tmp_path, ir=ir)
    index = build_session_index(accesses)
    frontier = query_session_frontier(
        ir, index, _block_at(ir, "protected", 11), accesses)
    guard = [c for group in frontier.path_constraint_groups for c in group
             if c.source_line == 9]
    assert guard
    assert guard[0].key_kind == KEY_UNKNOWN
    assert guard[0].key_expr == "?uid"


def test_frontier_serialisation_never_claims_verified():
    ir = build_icfg(GATE)
    accesses = extract_session_accesses(GATE, ir=ir)
    index = build_session_index(accesses)
    frontier = query_session_frontier(ir, index, TARGET, accesses)
    payload = frontier.to_dict()
    assert payload["evidence"] == "STATIC_MAY"
    assert all(d["evidence"] == "STATIC_MAY"
               for d in payload["candidate_writers"])
    assert "path_constraint_groups" in payload


def test_query_is_deterministic_for_same_inputs():
    ir = build_icfg(GATE)
    accesses = extract_session_accesses(GATE, ir=ir)
    index = build_session_index(accesses)
    first = query_session_frontier(ir, index, TARGET, accesses).to_dict()
    second = query_session_frontier(ir, index, TARGET, accesses).to_dict()
    assert first == second
