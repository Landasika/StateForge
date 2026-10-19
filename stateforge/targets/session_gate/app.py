"""SS-00 minimal Flask cookie-session target.

This module is a *target*, not a source of analyzer rules.  It deliberately
uses Flask's default client-side signed-cookie session: no database, no file
persistence, no JWT and no server-side session store.

Invariants for later Session-first batches
------------------------------------------
* Only the business routes mutate ``session``.  There is no request parameter
  that writes a session value directly.
* ``observe_session`` decodes the signed cookie currently held by a test
  client, read-only.  It does not open a request context, invoke a route or
  modify the client Cookie Jar.
* The fixed passcode is target configuration for single-request corpora.  No
  ordered multi-request sequence is exported by this module.

The guard variants (``*_protected`` routes) exist so later extraction tests
can see the supported source forms (``session.get``, truthiness, membership,
subscript read) without guessing from route names.  ``target_block`` belongs
to the primary ``/protected`` route only; variant markers are distinct blocks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from flask import Flask, current_app, request, session

# Fixed target configuration for single-request dictionaries/corpora.
DEFAULT_PASSCODE = "valid"
PASSCODE_CONFIG_KEY = "SESSION_GATE_PASSCODE"

# Deterministic, test-only key.  The target is local and holds no real data;
# a fixed key makes signed-cookie observations reproducible.
DEV_SECRET_KEY = "stateforge-session-gate-dev-only-not-for-production"

TARGET_HITS_KEY = "SESSION_GATE_TARGET_HITS"

# Read-only observation status vocabulary (deliberately three-valued).
SESSION_ABSENT = "absent"       # no session cookie in this client's jar
SESSION_DECODED = "decoded"     # signed cookie decoded to a mapping
SESSION_INVALID = "invalid"     # cookie present but signature/payload invalid
SESSION_UNKNOWN = "unknown"     # cannot observe (e.g. no signing serializer)


@dataclass(frozen=True)
class SessionSnapshot:
    """One read-only view of a client's signed session cookie.

    ``values`` is only meaningful when ``status == SESSION_DECODED``.  An
    absent cookie and a decoded empty mapping are intentionally different:
    ``session.clear()`` leaves a valid (usually empty) signed cookie, while a
    client that never talked to the app has no cookie at all.
    """

    status: str
    values: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None
    cookie_name: str = "session"

    @property
    def present(self) -> bool:
        return self.status != SESSION_ABSENT

    @property
    def verified(self) -> bool:
        return self.status == SESSION_DECODED

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


def create_app(passcode: str = DEFAULT_PASSCODE) -> Flask:
    """Build a fresh target application with an empty target-hit ledger.

    No module-level Flask app is created here: callers own the instance, so
    independent clients/apps never share a Cookie Jar or hit ledger.
    """

    app = Flask(__name__)
    app.secret_key = DEV_SECRET_KEY
    app.config[PASSCODE_CONFIG_KEY] = passcode
    app.config[TARGET_HITS_KEY] = []
    _register_routes(app)
    return app


# --------------------------------------------------------------- observation


def observe_session(client, app: Flask | None = None) -> SessionSnapshot:
    """Decode the client's current session cookie without mutating anything.

    This is the target's trusted read-only observation interface.  It reads
    the cookie *from the client jar* (the same artifact a next request would
    carry) and verifies it with the app's own session serializer.  A missing
    cookie, a bad signature and a valid empty payload remain distinguishable;
    unknown states are never collapsed into ``{}``.
    """

    app = app or client.application
    interface = app.session_interface
    cookie_name = interface.get_cookie_name(app)
    cookie = client.get_cookie(cookie_name)
    if cookie is None or not cookie.value:
        return SessionSnapshot(SESSION_ABSENT, cookie_name=cookie_name)

    serializer = interface.get_signing_serializer(app)
    if serializer is None:
        return SessionSnapshot(SESSION_UNKNOWN, cookie_name=cookie_name,
                               error="session interface has no serializer")
    try:
        payload = serializer.loads(cookie.value)
    except Exception as exc:                       # noqa: BLE001 - observation
        return SessionSnapshot(SESSION_INVALID, cookie_name=cookie_name,
                               error=type(exc).__name__)
    if not isinstance(payload, dict):
        return SessionSnapshot(SESSION_INVALID, cookie_name=cookie_name,
                               error="session payload is not a mapping")
    return SessionSnapshot(SESSION_DECODED, values=dict(payload),
                           cookie_name=cookie_name)


def target_hits(app: Flask) -> list[dict]:
    """Copy of the target-block ledger (never the live list)."""
    return [dict(item) for item in app.config.get(TARGET_HITS_KEY, [])]


# ------------------------------------------------------------------- markers


def _hit(block: str) -> None:
    current_app.config.setdefault(TARGET_HITS_KEY, []).append({"block": block})


def target_block() -> None:
    """Primary target block: reached only after the ``/protected`` guard."""
    _hit("target_block")


def truthy_target_block() -> None:
    _hit("truthy_target_block")


def membership_target_block() -> None:
    _hit("membership_target_block")


def subscript_target_block() -> None:
    _hit("subscript_target_block")


# -------------------------------------------------------------------- routes


def _register_routes(app: Flask) -> None:

    @app.post("/authenticate")
    def authenticate():
        # Credential check is immediate: a failed write must not persist a
        # session cookie.  The passcode is target config, never a session key.
        if request.form.get("passcode") != current_app.config[PASSCODE_CONFIG_KEY]:
            return "deny", 403
        session["uid"] = 7
        return "ok"

    @app.get("/protected")
    def protected():
        uid = session.get("uid")
        if uid is None:
            return "deny", 401
        target_block()
        return "TARGET_BLOCK"

    # Guard-form variants for extraction tests.  They share the ``uid`` key
    # but have distinct markers, so the primary target block stays unique.
    @app.get("/protected_truthy")
    def protected_truthy():
        uid = session.get("uid")
        if uid:
            truthy_target_block()
            return "TRUTHY_TARGET"
        return "deny", 401

    @app.get("/protected_membership")
    def protected_membership():
        if "uid" in session:
            membership_target_block()
            return "MEMBERSHIP_TARGET"
        return "deny", 401

    @app.get("/protected_subscript")
    def protected_subscript():
        try:
            uid = session["uid"]
        except KeyError:
            return "deny", 401
        subscript_target_block()
        return "SUBSCRIPT_TARGET"

    @app.post("/logout")
    def logout():
        session.pop("uid", None)
        return "ok"

    @app.post("/clear")
    def clear():
        session.clear()
        return "ok"

    @app.post("/noise")
    def noise():
        # A successful write to a different key: HTTP 200 must not be read as
        # evidence that the ``uid`` guard is satisfied.
        session["other"] = 7
        return "ok"
