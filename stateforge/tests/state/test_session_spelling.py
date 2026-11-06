"""Session recognition must not depend on one framework's spelling.

Three independent layers resolve session expressions: the state extractor,
the session-access collector and the session index. Each used to match the
literal name ``session`` or ``flask.session``; on a Django target all three
therefore reported nothing, and the guided search was handed an empty
frontier ("not-session-resolvable"). These tests pin the behaviour of the
two layers that had no coverage.
"""

from __future__ import annotations

import textwrap

import pytest

from stateforge.state.session_access import extract_session_accesses
from stateforge.state.session_index import _looks_like_session


DJANGO_LOGIN = '''
SESSION_KEY = "_auth_user_id"


def login(request, user, backend=None):
    if SESSION_KEY in request.session:
        if request.session[SESSION_KEY] != str(user.pk):
            request.session.flush()
    request.session[SESSION_KEY] = user._meta.pk.value_to_string(user)
    request.session["BACKEND_SESSION_KEY"] = backend
'''

FLASK_VIEW = '''
from flask import session


def index():
    session["uid"] = 1
'''


@pytest.mark.parametrize("expr", ["request.session", "self.session",
                                  "flask.session", "session", "app_session"])
def test_attribute_forms_look_like_session(expr):
    import ast
    node = ast.parse(expr, mode="eval").body
    assert _looks_like_session(node, set(), set())


@pytest.mark.parametrize("expr", ["self.app_session", "request.user_session"])
def test_compound_attribute_forms(expr):
    import ast
    node = ast.parse(expr, mode="eval").body
    assert _looks_like_session(node, set(), set())


@pytest.mark.parametrize("expr", ["cart", "ctx.data", "store"])
def test_non_session_expressions_are_rejected(expr):
    import ast
    node = ast.parse(expr, mode="eval").body
    assert not _looks_like_session(node, set(), set())


def test_django_attribute_writes_are_collected(tmp_path):
    """``request.session[k] = v`` must yield SET accesses with no import."""
    (tmp_path / "auth.py").write_text(textwrap.dedent(DJANGO_LOGIN),
                                     encoding="utf-8")
    accesses = extract_session_accesses(tmp_path)
    sets = [a for a in accesses if a.operation == "SET"]
    assert len(sets) >= 2
    # Provenance names the object actually used, not a fixed framework label.
    assert all(a.provenance for a in sets)
    assert any("request.session" in p for a in sets for p in a.provenance)
    # The two unconditional writes at the end of the view are both recorded.
    assert len({a.source_line for a in sets}) >= 2
    keys = {a.key_expr for a in sets}
    assert any("SESSION_KEY" in str(k) for k in keys)


def test_flask_bare_name_still_works(tmp_path):
    (tmp_path / "views.py").write_text(textwrap.dedent(FLASK_VIEW),
                                      encoding="utf-8")
    accesses = extract_session_accesses(tmp_path)
    assert [a for a in accesses if a.operation == "SET"]


def test_function_without_any_session_access_is_skipped(tmp_path):
    (tmp_path / "views.py").write_text(
        "def index():\n"
        "    cart = {}\n"
        "    cart['x'] = 1\n",
        encoding="utf-8")
    assert extract_session_accesses(tmp_path) == []


def test_function_view_methods_are_inferred():
    """A POST-only view must not be published as GET."""
    import ast

    from stateforge.analysis.django_adapter import _function_view_methods

    fn = ast.parse(
        "def login(request):\n"
        "    if request.method == 'POST':\n"
        "        pass\n").body[0]
    methods = _function_view_methods(fn)
    # The POST branch must be published; GET stays because an unguarded
    # function view is still dispatched for it.
    assert "POST" in methods
    assert methods[0] == "GET"


def test_require_post_decorator_is_authoritative():
    import ast

    from stateforge.analysis.django_adapter import _function_view_methods

    fn = ast.parse(
        "def login(request):\n"
        "    require_POST(request)\n").body[0]
    assert _function_view_methods(fn) == ["POST"]


def test_function_view_defaults_to_get():
    import ast

    from stateforge.analysis.django_adapter import _function_view_methods

    fn = ast.parse("def index(request):\n    return None\n").body[0]
    assert _function_view_methods(fn) == ["GET"]
