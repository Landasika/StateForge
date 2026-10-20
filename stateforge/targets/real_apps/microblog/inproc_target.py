"""In-process HTTP test-client adapter for microblog (SS-07F)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[4] / "external" / "microblog"
sys.path.insert(0, str(APP_ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/sf_microblog.db")
os.environ.setdefault("SECRET_KEY", "sf-microblog-test")

from microblog import app  # noqa: E402
from flask.sessions import SecureCookieSessionInterface  # noqa: E402

from stateforge.state.session_observation import snapshot_view  # noqa: E402

app.config["WTF_CSRF_ENABLED"] = False
_CLIENT = app.test_client()
_HITS = []


def reset_state():
    global _CLIENT
    _CLIENT = app.test_client()
    _HITS.clear()


def observe_session(client=None):
    raw = _CLIENT.get_cookie("session")
    if raw is None or not raw.value:
        return type("S", (), {"status": "absent", "values": None})()
    ser = SecureCookieSessionInterface().get_signing_serializer(app)
    try:
        payload = ser.loads(raw.value)
    except Exception:                              # noqa: BLE001
        return type("S", (), {"status": "invalid", "values": None})()
    return type("S", (), {"status": "decoded", "values": dict(payload)})()


def session_view(client=None):
    return snapshot_view(observe_session(), client_id="microblog-client",
                         observation_point="after_response",
                         source="flask-signed-cookie",
                         observer_id="microblog-signed-cookie-reader")


def target_hits():
    return list(_HITS)


def _request(step):
    method = str(step.get("method", "GET")).upper()
    path = str(step.get("path", "/"))
    kwargs = {}
    if isinstance(step.get("form"), dict):
        kwargs["data"] = {str(k): v for k, v in step["form"].items()}
    if isinstance(step.get("json"), dict):
        kwargs["json"] = step["json"]
    if isinstance(step.get("query"), dict):
        kwargs["query_string"] = step["query"]
    r = _CLIENT.open(path, method=method, **kwargs)
    if r.status_code == 200 and path.split("?")[0] in ("/", "/index"):
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
