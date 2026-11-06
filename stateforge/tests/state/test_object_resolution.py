"""Object resolution: session writes are found by object, not by variable name.

The extractor used to match the literal name ``session``, so Django's
``request.session[SESSION_KEY] = ...`` was invisible and the session-write
facts for a real application came out as zero. These tests pin the general
resolution so a spelling change cannot silently blind the extractor again.
"""

from __future__ import annotations

import ast

import pytest

from stateforge.state import objects
from stateforge.state.extractor import extract_state_facts


def _fn(source: str) -> ast.AST:
    body = "\n".join("    " + line for line in source.strip().splitlines())
    return ast.parse(f"def f(request, self):\n{body}\n").body[0]


def _first_subscript_object(source: str) -> str:
    fn = _fn(source)
    aliases = objects.Aliases.collect(fn)
    for node in ast.walk(fn):
        if isinstance(node, ast.Subscript):
            return objects.object_of_subscript(node, aliases)
    raise AssertionError("no subscript in source")


@pytest.mark.parametrize(("source", "expected"), [
    ('session["k"] = 1', "session"),
    ('request.session["k"] = 1', "request.session"),
    ('self.session["k"] = 1', "self.session"),
    ('flask.session["k"] = 1', "flask.session"),
    ('s = request.session\ns["k"] = 1', "request.session"),
    ('a = request.session\nb = a\nb["k"] = 1', "request.session"),
])
def test_alias_resolution(source, expected):
    assert _first_subscript_object(source) == expected


@pytest.mark.parametrize("name", ["session", "request.session", "self.session",
                                  "app_session", "user_sessions"])
def test_session_like_names(name):
    assert objects.is_session_like(name)


@pytest.mark.parametrize("name", ["cart", "ctx.data", "store", "self.state", ""])
def test_non_session_names(name):
    assert not objects.is_session_like(name)


def test_symbolic_key_is_expanded_from_module_constants():
    tree = ast.parse('SESSION_KEY = "_auth_user_id"\n')
    constants = objects.module_constants(tree)
    node = ast.parse("request.session[SESSION_KEY]", mode="eval").body
    assert objects.session_key(node, constants) == "_auth_user_id"


def test_unknown_symbolic_key_is_kept_not_dropped():
    """An unresolved key must still produce a fact; dropping it loses writes."""
    node = ast.parse("request.session[UNKNOWN]", mode="eval").body
    assert objects.session_key(node, {}) == "<UNKNOWN>"


def test_django_style_session_write_is_extracted(tmp_path):
    """The exact spelling Django uses must yield a WRITE fact."""
    (tmp_path / "auth.py").write_text(
        'SESSION_KEY = "_auth_user_id"\n'
        "\n"
        "def login(request, user):\n"
        "    if request.user.is_authenticated:\n"
        "        pass\n"
        "    request.session[SESSION_KEY] = user.pk\n",
        encoding="utf-8")
    facts = extract_state_facts(tmp_path)
    writes = [f for f in facts if f.domain.value.upper() == "SESSION"
              and f.operation.value.upper() == "WRITE"]
    assert len(writes) == 1
    assert writes[0].object_ref == "session._auth_user_id"
    assert writes[0].lineno == 6


def test_session_via_local_alias_is_extracted(tmp_path):
    (tmp_path / "views.py").write_text(
        "def set_token(request):\n"
        "    store = request.session\n"
        "    store['token'] = 'abc'\n",
        encoding="utf-8")
    facts = extract_state_facts(tmp_path)
    refs = {f.object_ref for f in facts
            if f.domain.value.upper() == "SESSION"}
    assert "session.token" in refs


def test_plain_dict_assignment_is_not_a_session_fact(tmp_path):
    (tmp_path / "views.py").write_text(
        "def f(request):\n"
        "    cart = {}\n"
        "    cart['x'] = 1\n",
        encoding="utf-8")
    facts = extract_state_facts(tmp_path)
    assert not [f for f in facts if f.domain.value.upper() == "SESSION"]


def test_session_write_binds_to_a_block(tmp_path):
    from stateforge.analysis.static.icfg import build_icfg

    (tmp_path / "views.py").write_text(
        "def login(request):\n"
        "    request.session['uid'] = 1\n",
        encoding="utf-8")
    ir = build_icfg(tmp_path)
    facts = extract_state_facts(tmp_path, ir=ir)
    writes = [f for f in facts if f.domain.value.upper() == "SESSION"
              and f.operation.value.upper() == "WRITE"]
    assert writes and writes[0].block_id
    assert writes[0].block_id in ir.blocks
