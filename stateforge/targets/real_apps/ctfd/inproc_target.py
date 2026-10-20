"""In-process HTTP test-client adapter for CTFd (real Flask app)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[4] / "external" / "CTFd"
sys.path.insert(0, str(APP_ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/sf_ctfd2.sqlite")
os.environ.setdefault("SECRET_KEY", "sf-ctfd-test")

from CTFd import create_app  # noqa: E402
from flask.sessions import SecureCookieSessionInterface  # noqa: E402

from stateforge.state.session_observation import snapshot_view  # noqa: E402

app = create_app()
_CLIENT = app.test_client()
_HITS = []


def reset_state():
    global _CLIENT
    _CLIENT = app.test_client()
    _HITS.clear()


def _nonce():
    with _CLIENT.session_transaction() as sess:
        if not sess.get("nonce"):
            import secrets
            sess["nonce"] = secrets.token_hex(32)
        return sess["nonce"]


def observe_session(client=None):
    raw = None
    jar = getattr(_CLIENT, "cookie_jar", None)
    for domain in getattr(jar, "_cookies", {}).values():
        for path_cookies in domain.values():
            if "session" in path_cookies:
                raw = path_cookies["session"].value
                break
    if not raw:
        return type("S", (), {"status": "absent", "values": None})()
    try:
        with app.test_request_context(headers={"Cookie": f"session={raw}"}):
            from flask import session as flask_session
            payload = dict(flask_session)
    except Exception:                              # noqa: BLE001
        return type("S", (), {"status": "invalid", "values": None})()
    return type("S", (), {"status": "decoded", "values": payload})()


def session_view(client=None):
    return snapshot_view(observe_session(), client_id="ctfd-client",
                         observation_point="after_response",
                         source="flask-signed-cookie",
                         observer_id="ctfd-signed-cookie-reader")


def target_hits():
    return list(_HITS)


def _request(step):
    method = str(step.get("method", "GET")).upper()
    path = str(step.get("path", "/"))
    kwargs = {}
    if isinstance(step.get("form"), dict):
        data = {str(k): v for k, v in step["form"].items()}
        if method == "POST":
            data.setdefault("nonce", _nonce())
        kwargs["data"] = data
    if isinstance(step.get("json"), dict):
        kwargs["json"] = step["json"]
    if isinstance(step.get("query"), dict):
        kwargs["query_string"] = step["query"]
    r = _CLIENT.open(path, method=method, **kwargs)
    if r.status_code == 200 and path.split("?")[0] in ("/challenges", "/"):
        _HITS.append({"path": path, "status": 200})
    return r


def target(data: bytes):
    try:
        step = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    if isinstance(step, dict) and isinstance(step.get("steps"), list):
        for s in step["steps"]:
            if isinstance(s, dict):
                _request(s)
    elif isinstance(step, dict):
        _request(step)
