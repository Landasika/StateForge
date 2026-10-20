"""In-process HTTP test-client adapter for Spirit (Django)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[4] / "external" / "Spirit"
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "test_project"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "test_project.settings.dev")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.test import Client  # noqa: E402

if "testserver" not in settings.ALLOWED_HOSTS:
    settings.ALLOWED_HOSTS.append("testserver")

from stateforge.state.session_observation import snapshot_view  # noqa: E402

_CLIENT = Client()
_HITS = []


def reset_state():
    global _CLIENT
    _CLIENT = Client()
    _HITS.clear()


def observe_session(client=None):
    try:
        values = dict(_CLIENT.session)
    except Exception:                              # noqa: BLE001
        return type("S", (), {"status": "absent", "values": None})()
    if not values:
        return type("S", (), {"status": "absent", "values": None})()
    return type("S", (), {"status": "decoded", "values": values})()


def session_view(client=None):
    return snapshot_view(observe_session(), client_id="spirit-client",
                         observation_point="after_response",
                         source="django-session",
                         observer_id="spirit-django-session-reader")


def target_hits():
    return list(_HITS)


def _request(step):
    method = str(step.get("method", "GET")).upper()
    path = str(step.get("path", "/"))
    data = {}
    if isinstance(step.get("form"), dict):
        data = {str(k): v for k, v in step["form"].items()}
    if isinstance(step.get("query"), dict):
        data.update({str(k): v for k, v in step["query"].items()})
    if method == "POST":
        r = _CLIENT.post(path, data=data)
    else:
        r = _CLIENT.get(path, data=data)
    if r.status_code == 200 and path.split("?")[0] == "/user/":
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
