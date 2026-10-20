"""Real-application HTTP adapter: microblog (Flask).

Fixture contract (SS-07F):
- app is launched by the operator with an isolated SQLite DB and a test user;
- this adapter uses a real HTTP client, a real Cookie Jar and the app's own
  signed-cookie serializer for read-only Session observation;
- CSRF is disabled only in the launch fixture, not by changing app source.

The adapter is plumbing, not a success chain: it forwards C10 single-request
dicts and does not inject credentials or target routes.
"""
from __future__ import annotations

import json
import os
import requests
from flask import Flask
from flask.sessions import SecureCookieSessionInterface

from stateforge.state.session_observation import snapshot_view

BASE = os.environ.get("SF_MICROBLOG_BASE", "http://127.0.0.1:5010")
SECRET = os.environ.get("SF_MICROBLOG_SECRET", "sf-microblog-test")
COOKIE = "session"
_SESSION = requests.Session()
_HITS: list[dict] = []


def reset_state() -> None:
    global _SESSION
    _SESSION = requests.Session()
    _HITS.clear()


def current_client():
    return _SESSION


def target_hits() -> list[dict]:
    return list(_HITS)


def _decode_cookie():
    raw = _SESSION.cookies.get(COOKIE)
    if not raw:
        return "absent", None
    app = Flask("sf-microblog-observer")
    app.secret_key = SECRET
    ser = SecureCookieSessionInterface().get_signing_serializer(app)
    try:
        payload = ser.loads(raw)
    except Exception:                              # noqa: BLE001
        return "invalid", None
    if not isinstance(payload, dict):
        return "invalid", None
    return "decoded", dict(payload)


def session_view(client=None):
    status, values = _decode_cookie()
    return snapshot_view(
        type("S", (), {"status": status, "values": values})(),
        client_id="microblog-client", observation_point="after_response",
        source="flask-signed-cookie",
        observer_id="microblog-signed-cookie-reader")


def observe_session(client=None):
    status, values = _decode_cookie()
    return type("S", (), {"status": status, "values": values})()


def _coerce(v):
    if isinstance(v, str) and v.isdigit():
        return int(v)
    return v


def target(data: bytes) -> None:
    try:
        step = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    if not isinstance(step, dict):
        return
    if isinstance(step.get("steps"), list):
        for s in step["steps"]:
            if isinstance(s, dict):
                _request(s)
        return
    _request(step)


def _request(step: dict) -> None:
    method = str(step.get("method", "GET")).upper()
    path = str(step.get("path", "/"))
    kwargs = {}
    if isinstance(step.get("form"), dict):
        kwargs["data"] = {str(k): _coerce(v)
                          for k, v in step["form"].items()}
    if isinstance(step.get("json"), dict):
        kwargs["json"] = {str(k): _coerce(v)
                         for k, v in step["json"].items()}
    if isinstance(step.get("query"), dict):
        kwargs["params"] = {str(k): _coerce(v)
                            for k, v in step["query"].items()}
    headers = step.get("header") or step.get("headers")
    if isinstance(headers, dict):
        kwargs["headers"] = {str(k): str(v) for k, v in headers.items()}
    try:
        r = _SESSION.request(method, BASE + path, timeout=5, **kwargs)
    except requests.RequestException:
        return
    if r.status_code == 200 and path.split("?")[0] in ("/", "/index"):
        _HITS.append({"path": path, "status": r.status_code})


def run_input(steps) -> None:
    reset_state()
    for s in steps:
        _request(s)
