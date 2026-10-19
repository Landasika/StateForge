"""SS-01: Session AST access extraction (position-level, BlockID-anchored)."""

from __future__ import annotations

from pathlib import Path

import pytest

from stateforge.analysis.static.icfg import build_icfg
from stateforge.state.session_access import (
    KEY_ANY,
    KEY_CONST,
    KEY_SYMBOLIC,
    OP_CLEAR,
    OP_DELETE,
    OP_READ,
    OP_READ_MODIFY_WRITE,
    OP_SET,
    OP_UNKNOWN,
    READ_GET,
    READ_MEMBERSHIP,
    READ_POP,
    READ_SUBSCRIPT,
    EFFECT_STATIC_MAY,
    extract_session_accesses,
    extract_session_facts,
)

ALL_FORMS = '''
from flask import session


def write_const():
    session["uid"] = 7


def write_variable(value):
    session["uid"] = value


def read_subscript():
    return session["uid"]


def read_get():
    return session.get("uid")


def read_get_default():
    return session.get("uid", None)


def exists_check():
    return "uid" in session


def delete_pop():
    return session.pop("uid", None)


def delete_del():
    del session["uid"]


def clear_all():
    session.clear()


def update_const():
    session.update({"uid": 9, "other": "x"})


def update_dynamic(mapping):
    session.update(mapping)


def read_modify_write():
    session["uid"] += 1


def symbolic(key):
    return session[key]


def dynamic_write(key, value):
    session[key] = value


def unknown_method():
    return session.rotate()
'''


def _write(tmp_path: Path, source: str, name: str = "app.py") -> Path:
    (tmp_path / name).write_text(source, encoding="utf-8")
    return tmp_path


def _by(accesses, **want):
    return [a for a in accesses if all(getattr(a, k) == v for k, v in want.items())]


def test_required_forms_are_extracted_with_unique_sites(tmp_path):
    root = _write(tmp_path, ALL_FORMS)
    accesses = extract_session_accesses(root)

    assert len(accesses) == len({a.site_id for a in accesses})

    # SET is a set, never also a READ at the same site.
    sets = _by(accesses, operation=OP_SET, key_expr="uid")
    assert sets and all(a.key_kind == KEY_CONST for a in sets)
    assert sets[0].value_expr == 7
    assert not _by(accesses, operation=OP_READ, source_line=sets[0].source_line)

    # All supported reads.
    assert _by(accesses, operation=OP_READ, read_kind=READ_SUBSCRIPT)
    assert _by(accesses, operation=OP_READ, read_kind=READ_GET)
    assert _by(accesses, operation=OP_READ, read_kind=READ_MEMBERSHIP)
    assert _by(accesses, operation=OP_READ, read_kind=READ_POP)

    # pop is READ+DELETE, never SET.
    pop_reads = [a for a in _by(accesses, operation=OP_READ, read_kind=READ_POP)]
    pop_deletes = [a for a in _by(accesses, operation=OP_DELETE, read_kind=READ_POP)]
    assert pop_reads and pop_deletes
    assert pop_reads[0].default_present is True
    assert pop_reads[0].default_expr is None
    assert not _by(accesses, operation=OP_SET, source_line=pop_reads[0].source_line)

    # del and clear.
    assert _by(accesses, operation=OP_DELETE, source_line=_line(ALL_FORMS, 'del session'))
    clears = _by(accesses, operation=OP_CLEAR)
    assert clears and clears[0].key_kind == KEY_ANY and clears[0].key_expr is None

    # update splits constant keys; dynamic update remains UNKNOWN.
    assert len(_by(accesses, operation=OP_SET, source_line=_line(ALL_FORMS, "session.update({\"uid\""))) >= 2
    assert _by(accesses, operation=OP_UNKNOWN, reason="dynamic-mapping-update")
    assert _by(accesses, operation=OP_UNKNOWN, reason="unsupported-session-method:rotate")

    # += needs the old value: RMW, not a plain SET.
    assert _by(accesses, operation=OP_READ_MODIFY_WRITE, key_expr="uid")

    # Dynamic keys stay symbolic, with the expression preserved.
    sym = _by(accesses, operation=OP_READ, key_kind=KEY_SYMBOLIC)
    assert sym and "?key" in sym[0].key_expr


def test_get_default_none_is_recorded_as_present(tmp_path):
    accesses = extract_session_accesses(_write(tmp_path, ALL_FORMS))
    access = _by(accesses, operation=OP_READ, read_kind=READ_GET,
                 source_line=_line(ALL_FORMS, 'session.get("uid", None)'))[0]
    assert access.default_present is True
    assert access.default_expr is None


def test_write_only_is_not_double_counted_as_read(tmp_path):
    root = _write(tmp_path, '''
from flask import session

def login():
    session["uid"] = 7
''')
    accesses = extract_session_accesses(root)
    uid = [a for a in accesses if a.key_expr == "uid"]
    assert [a.operation for a in uid] == [OP_SET]


def test_local_shadow_is_not_flask_session(tmp_path):
    root = _write(tmp_path, '''
from flask import session

def shadow_parameter(session):
    session = {}
    session["uid"] = 7
    return session.get("uid")

def shadow_local():
    session = {}
    session["uid"] = 7
    return session.get("uid")
''')
    assert extract_session_accesses(root) == []


def test_import_alias_and_flask_attribute_are_recognised(tmp_path):
    root = _write(tmp_path, '''
from flask import session as sess
import flask
import flask as f

def via_import_alias():
    sess["uid"] = 7

def via_module():
    return flask.session.get("uid")

def via_module_alias():
    return f.session["uid"]
''')
    accesses = extract_session_accesses(root)
    assert _by(accesses, operation=OP_SET, key_expr="uid")
    assert len(_by(accesses, operation=OP_READ, key_expr="uid")) == 2


def test_simple_local_alias_is_propagated(tmp_path):
    root = _write(tmp_path, '''
from flask import session

def login():
    s = session
    s["uid"] = 7
    return s.get("uid")
''')
    accesses = extract_session_accesses(root)
    assert _by(accesses, operation=OP_SET, key_expr="uid")
    assert _by(accesses, operation=OP_READ, key_expr="uid", read_kind=READ_GET)


def test_business_names_are_not_hardcoded(tmp_path):
    root = _write(tmp_path, '''
from flask import session

def completely_different_name():
    session["ticket"] = 41

def another_name():
    ticket = session.get("ticket")
    if ticket is None:
        return "deny"
    return "ok"
''')
    accesses = extract_session_accesses(root)
    assert _by(accesses, operation=OP_SET, key_expr="ticket")
    assert _by(accesses, operation=OP_READ, key_expr="ticket")


def test_nested_function_is_not_double_counted(tmp_path):
    root = _write(tmp_path, '''
from flask import session

def outer():
    def inner():
        return session.get("uid")
    return inner()
''')
    reads = _by(extract_session_accesses(root), operation=OP_READ, key_expr="uid")
    assert len(reads) == 1


def test_static_events_are_never_verified(tmp_path):
    accesses = extract_session_accesses(_write(tmp_path, ALL_FORMS))
    assert accesses
    assert all(a.effect_status == EFFECT_STATIC_MAY for a in accesses
               if a.operation != OP_UNKNOWN)


def test_accesses_bind_to_program_ir_blocks():
    target = (Path(__file__).resolve().parents[2] / "targets" / "session_gate")
    ir = build_icfg(target)
    accesses = extract_session_accesses(target, ir=ir)

    writer = [a for a in accesses if a.operation == OP_SET and a.key_expr == "uid"]
    assert writer
    assert writer[0].block_id == "app.py::authenticate::165-165::block::1"

    guard_read = [a for a in accesses
                  if a.operation == OP_READ and a.read_kind == READ_GET
                  and "::protected::" in (a.block_id or "")]
    assert guard_read
    assert guard_read[0].block_id == "app.py::protected::170-170::block::1"


def test_session_facts_are_adapted_from_the_same_events(tmp_path):
    root = _write(tmp_path, ALL_FORMS)
    accesses, facts = extract_session_facts(root)
    assert len(facts) == len([a for a in accesses if a.operation != OP_UNKNOWN])
    assert any(f.object_ref == "session.uid" for f in facts)
    assert any(f.object_ref == "session.*" for f in facts)
    assert all(f.domain.value == "SESSION" for f in facts)
    assert len({f.candidate_id for f in facts}) == len(facts)


def test_clear_is_any_and_not_a_constant_key(tmp_path):
    root = _write(tmp_path, '''
from flask import session

def clear_all():
    session.clear()
''')
    access = extract_session_accesses(root)[0]
    assert access.operation == OP_CLEAR
    assert access.key_kind == KEY_ANY
    assert access.key_expr is None


def _line(source: str, needle: str) -> int:
    for i, line in enumerate(source.splitlines(), 1):
        if needle in line:
            return i
    raise AssertionError(f"needle not found: {needle}")
