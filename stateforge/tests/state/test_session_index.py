"""SS-02: SessionAccessIndex + target frontier query (STATIC_MAY)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from stateforge.analysis.static.icfg import build_icfg
from stateforge.state.session_access import (BACKEND_FLASK_SESSION, KEY_ANY,
                                             KEY_CONST, OP_CLEAR, OP_DELETE,
                                             OP_READ, OP_SET,
                                             extract_session_accesses)
from stateforge.state.session_index import (COND_EXISTS, COND_NOT_EXISTS,
                                            MAY_OVERLAP, MUST_OVERLAP,
                                            SessionAccessIndex,
                                            build_session_index,
                                            query_session_frontier)

GATE = Path(__file__).resolve().parents[2] / "targets" / "session_gate"
GATE_TARGET = "app.py::protected::173-173::block::1"


def _write(tmp_path: Path, source: str) -> Path:
    (tmp_path / "app.py").write_text(source, encoding="utf-8")
    return tmp_path


def _block_at(ir, function: str, line: int) -> str:
    hits = [b for b in ir.blocks.values()
            if b.function == function and b.start_line <= line <= b.end_line]
    exact = [b for b in hits if b.start_line == line
             and b.kind in ("terminal", "block", "branch")]
    assert exact or hits, f"no block for {function}:{line}"
    return (exact or hits)[0].id


def _gate_frontier():
    ir = build_icfg(GATE)
    accesses = extract_session_accesses(GATE, ir=ir)
    index = build_session_index(accesses)
    return ir, accesses, index, query_session_frontier(ir, index, GATE_TARGET,
                                                       accesses)


def test_gate_frontier_finds_uid_writer_and_destructive_candidates():
    ir, accesses, index, frontier = _gate_frontier()

    assert not frontier.partial
    assert frontier.evidence == "STATIC_MAY"
    assert frontier.path_constraint_groups
    condition = frontier.path_constraint_groups[0][0]
    assert (condition.key_expr, condition.op) == ("uid", COND_EXISTS)

    blocks = set(frontier.candidate_blocks)
    assert "app.py::authenticate::165-165::block::1" in blocks   # uid=7
    assert "app.py::logout::204-204::block::1" in blocks         # pop uid
    assert "app.py::clear::209-209::block::1" in blocks          # clear any

    effects = {(d.candidate_effect["operation"],
                d.candidate_effect.get("key_expr")) for d in frontier.candidate_writers}
    assert ("SET", "uid") in effects
    assert ("CLEAR", None) in effects
    assert ("DELETE", "uid") in effects
    # The `other` key writer must not be offered for the uid condition.
    assert ("SET", "other") not in effects

    writers = {d.writer_site_id: d for d in frontier.candidate_writers}
    assert all(d.evidence == "STATIC_MAY" for d in frontier.candidate_writers)
    assert any(d.key_overlap == MUST_OVERLAP for d in frontier.candidate_writers)
    assert any(d.key_overlap == MAY_OVERLAP for d in frontier.candidate_writers)
    assert all("?client" in d.client_binding for d in frontier.candidate_writers)


def test_other_key_only_writer_is_not_offered(tmp_path):
    root = _write(tmp_path, '''
from flask import session

def authenticate():
    session["other"] = 7

def protected():
    uid = session.get("uid")
    if uid is None:
        return "deny"
    return "ok"
''')
    ir = build_icfg(root)
    accesses = extract_session_accesses(root, ir=ir)
    index = build_session_index(accesses)
    target = _block_at(ir, "protected", 11)
    frontier = query_session_frontier(ir, index, target, accesses)

    assert frontier.path_constraint_groups
    assert frontier.candidate_blocks == ()
    assert frontier.partial is True
    assert any(u.cause == "no-static-session-writer"
               for u in frontier.unresolved)


def test_dynamic_writer_is_may_overlap_not_must(tmp_path):
    root = _write(tmp_path, '''
from flask import session

def authenticate(key):
    session[key] = 7

def protected():
    if session.get("uid") is None:
        return "deny"
    return "ok"
''')
    ir = build_icfg(root)
    accesses = extract_session_accesses(root, ir=ir)
    index = build_session_index(accesses)
    frontier = query_session_frontier(ir, index, _block_at(ir, "protected", 10),
                                      accesses)

    assert frontier.candidate_writers
    assert all(d.key_overlap == MAY_OVERLAP for d in frontier.candidate_writers)


def test_not_exists_prioritises_delete_and_clear_over_set(tmp_path):
    root = _write(tmp_path, '''
from flask import session

def login():
    session["uid"] = 7

def logout():
    session.pop("uid", None)

def clear_all():
    session.clear()

def protected():
    if "uid" in session:
        return "present"
    return "absent"
''')
    ir = build_icfg(root)
    accesses = extract_session_accesses(root, ir=ir)
    index = build_session_index(accesses)
    frontier = query_session_frontier(ir, index, _block_at(ir, "protected", 16),
                                      accesses)

    assert frontier.path_constraint_groups
    cond = frontier.path_constraint_groups[0][0]
    assert cond.op == COND_NOT_EXISTS

    by_op = {}
    for d in frontier.candidate_writers:
        by_op.setdefault(d.candidate_effect["operation"], []).append(d.priority)
    assert min(by_op["DELETE"]) == 0
    assert min(by_op["CLEAR"]) == 0
    assert min(by_op["SET"]) == 3


def test_multiple_paths_keep_separate_groups(tmp_path):
    root = _write(tmp_path, '''
from flask import session

def a():
    session["uid"] = 1

def b():
    session["token"] = 2

def protected():
    if session.get("mode") == "a":
        if session.get("uid"):
            pass
        else:
            return "deny"
    else:
        if session.get("token"):
            pass
        else:
            return "deny"
    return "target"
''')
    ir = build_icfg(root)
    accesses = extract_session_accesses(root, ir=ir)
    index = build_session_index(accesses)
    frontier = query_session_frontier(ir, index, _block_at(ir, "protected", 21),
                                      accesses)

    groups = [[c.canonical for c in group]
              for group in frontier.path_constraint_groups]
    assert len(groups) == 2
    assert any(any("session.uid TRUTHY" in c for c in g) for g in groups)
    assert any(any("session.token TRUTHY" in c for c in g) for g in groups)
    # Not a single merged AND of both alternatives.
    assert not any(all("session.uid" in c and "session.token" in c for c in g)
                   for g in groups)
    assert _block_at(ir, "a", 5) in frontier.candidate_blocks
    assert _block_at(ir, "b", 8) in frontier.candidate_blocks


def test_alias_guard_condition_resolves_to_session_key(tmp_path):
    root = _write(tmp_path, '''
from flask import session

def login():
    session["uid"] = 7

def protected():
    uid = session.get("uid")
    if uid is None:
        return "deny"
    return "ok"
''')
    ir = build_icfg(root)
    accesses = extract_session_accesses(root, ir=ir)
    index = build_session_index(accesses)
    frontier = query_session_frontier(ir, index, _block_at(ir, "protected", 11),
                                      accesses)

    assert frontier.path_constraint_groups[0][0].key_expr == "uid"
    assert frontier.path_constraint_groups[0][0].op == COND_EXISTS
    assert _block_at(ir, "login", 5) in frontier.candidate_blocks


def test_non_session_guard_is_unresolved_not_guessed(tmp_path):
    root = _write(tmp_path, '''
def protected():
    if some_check():
        return "x"
    return "target"
''')
    ir = build_icfg(root)
    accesses = extract_session_accesses(root, ir=ir)
    index = build_session_index(accesses)
    frontier = query_session_frontier(ir, index, _block_at(ir, "protected", 5),
                                      accesses)

    assert all(not group for group in frontier.path_constraint_groups)
    assert frontier.partial is True
    assert any(u.cause == "not-session-resolvable" for u in frontier.unresolved)


def test_index_queries_one_reader_not_global_cartesian_product():
    ir, accesses, index, frontier = _gate_frontier()
    readers = index.readers("uid")
    assert readers
    deps = index.writers_of(readers[0])
    assert deps
    assert all(d.reader_site_id == readers[0].site_id for d in deps)
    # No precomputed writer×reader edges: only the queried reader has deps.
    other_reader = next(a for a in index.readers("uid")
                        if a.site_id != readers[0].site_id)
    other_deps = index.writers_of(other_reader)
    assert all(d.reader_site_id == other_reader.site_id for d in other_deps)


def test_clear_is_any_key_writer():
    ir, accesses, index, frontier = _gate_frontier()
    clear = next(a for a in accesses if a.operation == OP_CLEAR)
    assert clear.key_kind == KEY_ANY
    deps = index.writers_of(clear, key_kind="CONST", key_expr="uid")
    assert any(d.candidate_effect["operation"] == OP_CLEAR for d in deps)


def test_missing_target_is_partial_not_exception():
    ir, _accesses, index, _frontier = _gate_frontier()
    frontier = query_session_frontier(ir, index, "missing::block::1")
    assert frontier.partial is True
    assert frontier.unresolved[0].cause == "target-block-not-found"


def test_backend_is_part_of_the_index_bucket():
    _ir, accesses, _index, _frontier = _gate_frontier()
    writer = next(a for a in accesses
                  if a.operation == OP_SET and a.key_expr == "uid")
    other = dataclasses.replace(writer, backend="other.session",
                                site_id=writer.site_id + "-other")
    index = SessionAccessIndex([writer, other])
    assert len(index.by_key) == 2
    flask = index.writers_for_key(KEY_CONST, "uid",
                                  backend=BACKEND_FLASK_SESSION)
    other_only = index.writers_for_key(KEY_CONST, "uid",
                                       backend="other.session")
    assert writer in flask and other not in flask
    assert other in other_only and writer not in other_only
