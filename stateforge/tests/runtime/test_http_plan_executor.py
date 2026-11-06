import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from stateforge.core.model import PlanSegment, PlanStep
from stateforge.runtime import http_plan_executor as executor


class FakeTraceSession:
    def __init__(self, root):
        self.root = root
        self.events = []
        self._request_id = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @contextmanager
    def request(self):
        self._request_id += 1
        rid = self._request_id
        self.events.append(SimpleNamespace(request_id=rid))
        yield rid


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class FakeClient:
    def __init__(self, statuses):
        self.statuses = iter(statuses)
        self.calls = []
        self.authenticated = False

    def get(self, path, **extra):
        self.calls.append(("get", path, extra))
        return FakeResponse(next(self.statuses))

    def post(self, path, data, **extra):
        self.calls.append(("post", path, data, extra))
        self.authenticated = True
        return FakeResponse(next(self.statuses))

    def generic(self, method, path, data, content_type, **extra):
        self.calls.append(("generic", method, path, data, content_type, extra))
        return FakeResponse(next(self.statuses))


@pytest.fixture(autouse=True)
def fake_tracing(monkeypatch):
    monkeypatch.setattr(executor, "TraceSession", FakeTraceSession)
    monkeypatch.setattr(
        executor, "map_trace",
        lambda events, ir: SimpleNamespace(
            blocks=["target"] if events and events[0].request_id == 2 else []))


def test_form_login_establishes_session_and_reaches_target(tmp_path):
    client = FakeClient([302, 200])
    segment = PlanSegment(goal="auth", steps=[
        PlanStep(action="POST /login/", form={"username": "x", "password": "x"}),
        PlanStep(action="GET /bookmarks/<int:bookmark_id>/edit"),
    ])
    result = executor.execute_app_chain(
        segment, client, SimpleNamespace(), "target", tmp_path,
        credentials={"username": "probe", "password": "Passw0rd!"},
        auth_probe=lambda c: c.authenticated)
    assert client.calls[0] == (
        "post", "/login/",
        {"username": "probe", "password": "Passw0rd!"}, {})
    assert client.calls[1][0:2] == ("get", "/bookmarks/1/edit")
    assert result.steps[0].session_authenticated is True
    assert result.target_covered and result.verified


def test_json_request_uses_application_json(tmp_path, monkeypatch):
    monkeypatch.setattr(
        executor, "map_trace",
        lambda events, ir: SimpleNamespace(blocks=["target"]))
    client = FakeClient([201])
    result = executor.execute_app_chain(
        PlanSegment(goal="json", steps=[
            PlanStep(action="PATCH /api/items", json={"title": "测试"},
                     headers={"X-Api-Key": "key"})]),
        client, SimpleNamespace(), "target", tmp_path)
    call = client.calls[0]
    assert call[:3] == ("generic", "PATCH", "/api/items")
    assert json.loads(call[3]) == {"title": "测试"}
    assert call[4] == "application/json"
    assert call[5] == {"HTTP_X_API_KEY": "key"}
    assert result.verified


@pytest.mark.parametrize(("status", "ok"), [(302, True), (401, False), (404, False)])
def test_http_success_classification(status, ok, tmp_path, monkeypatch):
    monkeypatch.setattr(
        executor, "map_trace",
        lambda events, ir: SimpleNamespace(blocks=["target"]))
    result = executor.execute_app_chain(
        PlanSegment(goal="status", steps=[PlanStep(action="POST /action")]),
        FakeClient([status]), SimpleNamespace(), "target", tmp_path)
    assert result.steps[0].ok is ok
    assert result.verified is ok


def test_target_miss_fails_the_whole_chain(tmp_path, monkeypatch):
    monkeypatch.setattr(
        executor, "map_trace",
        lambda events, ir: SimpleNamespace(blocks=[]))
    result = executor.execute_app_chain(
        PlanSegment(goal="miss", steps=[PlanStep(action="GET /target")]),
        FakeClient([200]), SimpleNamespace(), "target", tmp_path)
    assert result.steps[0].ok
    assert not result.target_covered
    assert not result.verified
