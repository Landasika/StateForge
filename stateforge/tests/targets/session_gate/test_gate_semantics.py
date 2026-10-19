"""SS-00 target-only semantics tests.

These tests exercise the Flask target and its C10 adapter.  They do not call
any state/session analyzer: target coverage is read from a real
``TraceSession`` over ``app.py``, and session truth is read from the signed
cookie held by the client jar (never by mutating session state in a route).
"""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from pathlib import Path

import pytest
from flask.sessions import SecureCookieSessionInterface

from stateforge.fuzzing.entry_driver import dispatch_seed, encode_sequence_seed
from stateforge.runtime.tracer import TraceSession
from stateforge.targets.session_gate import app as gate_app

APP_DIR = Path(gate_app.__file__).resolve().parent
TARGET_FILE = APP_DIR / "fuzz_target.py"


# ------------------------------------------------------------------ helpers


def _new_app():
    return gate_app.create_app()


def _cookie(client, app=None):
    return gate_app.observe_session(client, app)


def _covered(events, marker: str) -> bool:
    return any(e.function == marker and e.event_type == "call" for e in events)


def _traced_request(client, method: str, path: str, **kwargs):
    """Execute one real request under a TraceSession scoped to the target."""

    trace = TraceSession(root=APP_DIR)
    with trace:
        response = client.open(path, method=method, **kwargs)
    return response, trace.events


def _load_adapter():
    """Load a fresh adapter module, as entry_driver would (isolated globals)."""

    spec = importlib.util.spec_from_file_location(
        f"sf_session_gate_adapter_{uuid.uuid4().hex[:8]}", TARGET_FILE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _step(method: str, path: str, **kwargs) -> bytes:
    return json.dumps({"method": method, "path": path, **kwargs},
                      sort_keys=True).encode()


# --------------------------------------------------- primary target semantics


def test_anonymous_protected_is_401_and_target_not_covered():
    app = _new_app()
    client = app.test_client()

    response, events = _traced_request(client, "GET", "/protected")

    assert response.status_code == 401
    assert response.get_data(as_text=True) == "deny"
    assert not _covered(events, "target_block")
    assert gate_app.target_hits(app) == []
    assert _cookie(client, app).status == gate_app.SESSION_ABSENT


def test_valid_write_then_read_same_client_reaches_target():
    app = _new_app()
    client = app.test_client()

    write = client.post("/authenticate", data={"passcode": "valid"})
    assert write.status_code == 200
    assert isinstance(app.session_interface, SecureCookieSessionInterface)

    snapshot = _cookie(client, app)
    assert snapshot.status == gate_app.SESSION_DECODED
    assert snapshot.values == {"uid": 7}
    assert client.get_cookie("session") is not None

    response, events = _traced_request(client, "GET", "/protected")
    assert response.status_code == 200
    assert response.get_data(as_text=True) == "TARGET_BLOCK"
    assert _covered(events, "target_block")
    assert gate_app.target_hits(app) == [{"block": "target_block"}]


def test_invalid_passcode_writes_no_session_and_does_not_unlock():
    app = _new_app()
    client = app.test_client()

    write = client.post("/authenticate", data={"passcode": "wrong"})
    assert write.status_code == 403
    assert _cookie(client, app).status == gate_app.SESSION_ABSENT

    response, events = _traced_request(client, "GET", "/protected")
    assert response.status_code == 401
    assert not _covered(events, "target_block")
    assert gate_app.target_hits(app) == []


def test_write_to_other_key_does_not_satisfy_uid_guard():
    app = _new_app()
    client = app.test_client()

    write = client.post("/noise")
    assert write.status_code == 200
    snapshot = _cookie(client, app)
    assert snapshot.status == gate_app.SESSION_DECODED
    assert snapshot.values == {"other": 7}
    assert "uid" not in snapshot.values

    response, events = _traced_request(client, "GET", "/protected")
    assert response.status_code == 401
    assert not _covered(events, "target_block")
    assert gate_app.target_hits(app) == []


@pytest.mark.parametrize("revoke", ["/logout", "/clear"])
def test_logout_and_clear_revoke_previous_verified_session(revoke):
    app = _new_app()
    client = app.test_client()
    client.post("/authenticate", data={"passcode": "valid"})
    assert _cookie(client, app).values["uid"] == 7

    response, events = _traced_request(client, "GET", "/protected")
    assert response.status_code == 200 and _covered(events, "target_block")

    assert client.post(revoke).status_code == 200
    after = _cookie(client, app)
    # Flask deletes the signed cookie for an emptied session; accept either a
    # deleted cookie or a decoded payload without uid, but never a stale uid.
    assert after.status in (gate_app.SESSION_ABSENT, gate_app.SESSION_DECODED)
    assert "uid" not in after.values

    response, events = _traced_request(client, "GET", "/protected")
    assert response.status_code == 401
    assert not _covered(events, "target_block")


def test_two_independent_clients_do_not_share_cookie_jar():
    app = _new_app()
    client_a = app.test_client()
    client_b = app.test_client()

    assert client_a.post("/authenticate",
                         data={"passcode": "valid"}).status_code == 200
    assert _cookie(client_a, app).values["uid"] == 7

    denied, denied_events = _traced_request(client_b, "GET", "/protected")
    assert denied.status_code == 401
    assert not _covered(denied_events, "target_block")
    assert _cookie(client_b, app).status == gate_app.SESSION_ABSENT

    allowed, allowed_events = _traced_request(client_a, "GET", "/protected")
    assert allowed.status_code == 200
    assert _covered(allowed_events, "target_block")


def test_http_2xx_is_not_target_block_coverage():
    """2xx routes exist, but only the guarded target block counts as target."""

    app = _new_app()
    client = app.test_client()
    for method, path in [("POST", "/noise"), ("POST", "/logout"),
                         ("POST", "/clear"), ("POST", "/authenticate")]:
        response, events = _traced_request(
            client, method, path,
            **({"data": {"passcode": "wrong"}} if path == "/authenticate" else {}))
        # /authenticate with a wrong passcode is not 2xx; the others are.
        if path == "/authenticate":
            assert response.status_code == 403
        else:
            assert 200 <= response.status_code < 300
        assert not _covered(events, "target_block")
    assert gate_app.target_hits(app) == []


# ------------------------------------------------------- guard-form variants


def test_guard_variants_reach_only_their_own_blocks():
    app = _new_app()
    client = app.test_client()
    client.post("/authenticate", data={"passcode": "valid"})

    variants = [("/protected_truthy", "truthy_target_block"),
                ("/protected_membership", "membership_target_block"),
                ("/protected_subscript", "subscript_target_block")]
    for path, marker in variants:
        response, events = _traced_request(client, "GET", path)
        assert response.status_code == 200
        assert _covered(events, marker)
        assert not _covered(events, "target_block")

    client.post("/clear")
    for path, marker in variants:
        response, events = _traced_request(client, "GET", path)
        assert response.status_code == 401
        assert not _covered(events, marker)


# ------------------------------------------------------- adapter / isolation


def test_reset_state_replaces_client_and_drops_previous_session():
    mod = _load_adapter()
    mod.reset_state()
    old_client = mod.current_client()

    mod.target(_step("POST", "/authenticate", form={"passcode": "valid"}))
    assert mod.observe_session().values["uid"] == 7

    mod.reset_state()
    new_client = mod.current_client()
    assert new_client is not old_client
    assert old_client.get_cookie("session") is not None   # old jar untouched
    assert new_client.get_cookie("session") is None       # new jar clean
    assert mod.observe_session().status == gate_app.SESSION_ABSENT

    mod.target(_step("GET", "/protected"))
    assert mod.target_hits() == []
    assert mod.current_client().get("/protected").status_code == 401


def test_input_scope_keeps_cookie_within_input_and_cleans_at_exit():
    mod = _load_adapter()

    with mod.input_scope():
        mod.target(_step("POST", "/authenticate", form={"passcode": "valid"}))
        mod.target(_step("GET", "/protected"))
        assert mod.observe_session().values["uid"] == 7
        assert mod.target_hits() == [{"block": "target_block"}]

    # Same fuzz input ended: clean client, no carry-over cookie.
    assert mod.observe_session().status == gate_app.SESSION_ABSENT
    response = mod.current_client().get("/protected")
    assert response.status_code == 401
    assert mod.target_hits() == [{"block": "target_block"}]  # no new hit


def test_c10_sequence_seed_uses_one_client_for_all_steps():
    mod = _load_adapter()
    mod.reset_state()

    seed = encode_sequence_seed([
        {"method": "POST", "path": "/authenticate",
         "form": {"passcode": "valid"}},
        {"method": "GET", "path": "/protected"},
    ])
    dispatch_seed(mod.target, seed)

    assert mod.target_hits() == [{"block": "target_block"}]
    assert mod.observe_session().values["uid"] == 7

    # A fresh input containing only the read step cannot borrow that cookie.
    mod.reset_state()
    dispatch_seed(mod.target,
                  encode_sequence_seed([{"method": "GET",
                                         "path": "/protected"}]))
    assert mod.target_hits() == []
    assert mod.observe_session().status == gate_app.SESSION_ABSENT


def test_adapter_ignores_input_supplied_session_cookie():
    """A fuzz input cannot inject a signed session cookie to unlock the gate."""

    mod = _load_adapter()
    mod.reset_state()
    serializer = mod._APP.session_interface.get_signing_serializer(mod._APP)
    forged = serializer.dumps({"uid": 7})

    mod.target(_step("GET", "/protected", cookie={"session": forged}))

    assert mod.target_hits() == []
    assert mod.observe_session().status == gate_app.SESSION_ABSENT
    assert mod.current_client().get("/protected").status_code == 401


def test_observation_is_read_only_and_does_not_hit_target():
    app = _new_app()
    client = app.test_client()
    client.post("/authenticate", data={"passcode": "valid"})
    before = client.get_cookie("session").value

    snapshot = _cookie(client, app)

    assert snapshot.values == {"uid": 7}
    assert client.get_cookie("session").value == before
    assert gate_app.target_hits(app) == []
