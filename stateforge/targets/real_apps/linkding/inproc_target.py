"""Isolated C10/Django target for the real linkding application."""
from __future__ import annotations

import json

from stateforge.state.session_observation import snapshot_view
from stateforge.targets.apps20.linkding import harness


_CLIENT = harness.build_client(authenticated=False)
_HITS: list[dict] = []
_RESPONSES: list[dict] = []


def reset_state():
    """Reset between fuzz inputs while preserving state inside one C10 sequence."""
    global _CLIENT
    _CLIENT = harness.build_client(authenticated=False)
    _HITS.clear()
    _RESPONSES.clear()


def is_authenticated() -> bool:
    return harness.is_authenticated(_CLIENT)


def observe_session(client=None):
    try:
        values = dict(_CLIENT.session)
    except Exception:                              # noqa: BLE001
        return type("S", (), {"status": "absent", "values": None})()
    if not values:
        return type("S", (), {"status": "absent", "values": None})()
    return type("S", (), {"status": "decoded", "values": values})()


def session_view(client=None):
    return snapshot_view(observe_session(), client_id="linkding-client",
                         observation_point="after_response",
                         source="django-session",
                         observer_id="linkding-django-session-reader")


def target_hits():
    return list(_HITS)


def response_log():
    return list(_RESPONSES)


def _request(step):
    method = str(step.get("method", "GET")).upper()
    path = str(step.get("path", "/"))
    query = step.get("query") if isinstance(step.get("query"), dict) else {}
    form = step.get("form") if isinstance(step.get("form"), dict) else {}
    json_body = step.get("json") if isinstance(step.get("json"), dict) else None
    headers = step.get("header") if isinstance(step.get("header"), dict) else {}
    extra = {f"HTTP_{str(k).replace('-', '_').upper()}": v
             for k, v in headers.items()}
    if method == "GET":
        response = _CLIENT.get(path, data=query, **extra)
    elif form:
        if method == "POST":
            response = _CLIENT.post(path, data=form, **extra)
        else:
            response = _CLIENT.generic(method, path, data=form, **extra)
    else:
        response = _CLIENT.generic(
            method, path, data=json.dumps(json_body or {}),
            content_type="application/json", **extra)
    record = {"method": method, "path": path,
              "status": response.status_code,
              "authenticated": is_authenticated()}
    _RESPONSES.append(record)
    expected = f"/bookmarks/{harness.BM_ID}/edit"
    if response.status_code == 200 and path.split("?", 1)[0] == expected:
        _HITS.append(record)
    return response


def target(data: bytes):
    try:
        step = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    if isinstance(step, dict):
        _request(step)
