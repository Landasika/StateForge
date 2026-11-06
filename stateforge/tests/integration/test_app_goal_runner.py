"""Real-application pipeline contracts: stage evidence, isolation, cold replay.

These use a fake application so they run anywhere; the linkding end-to-end
run lives in ``tests/evaluation/test_real_app_goal.py`` and requires the app's
own interpreter.
"""

from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from stateforge.core.model import PlanSegment, PlanStep
from stateforge.pipeline import app_goal_runner as runner_module
from stateforge.pipeline.app_goal_runner import AppGoalRunner
from stateforge.solving.goal_checkpoint import GoalCheckpoint
from stateforge.state.goal_app_bridge import django_auth_completion

APP = "fakeapp"
BLOCK = "views.py::edit::10-12::block::1"


# --------------------------------------------------------------- fake target


class FakeApp:
    """Minimal Django-like surface: /login/ sets a session, edit needs it."""

    def __init__(self, *, login_works=True, edit_open=False):
        self.login_works = login_works
        self.edit_open = edit_open


def _build_ir(tmp_path: Path):
    source = tmp_path / "app"
    (source / "views").mkdir(parents=True, exist_ok=True)
    (source / "views" / "views.py").write_text(
        "from django.contrib.auth.decorators import login_required\n"
        "\n"
        "@login_required\n"
        "def edit(request, pk):\n"
        "    bookmark = get_object(request, pk)\n"
        "    if bookmark is None:\n"
        "        return 404\n"
        "    return render(bookmark)\n",
        encoding="utf-8")
    (source / "views" / "urls.py").write_text(
        "from django.contrib.auth import urls as auth_urls\n", encoding="utf-8")
    from stateforge.analysis.static.icfg import build_icfg
    return build_icfg(source / "views")


@pytest.fixture
def app_root(tmp_path):
    root = tmp_path / "app"
    (root / "views").mkdir(parents=True, exist_ok=True)
    return root


def _install_fake_client(monkeypatch, app: FakeApp, ir, *, reached_on_final_only=False):
    """Patch the analysis bridge, the harness module and the HTTP surface."""
    import sys
    import types

    from stateforge.analysis.analyzer import AppModel
    from stateforge.core.model import Effect, Endpoint, Param, Precondition

    harness = types.ModuleType("fake.harness")
    harness.build_client = lambda authenticated=False: SimpleNamespace(
        authenticated=authenticated)
    harness.is_authenticated = lambda client: bool(client.authenticated)
    package = types.ModuleType("fake")
    package.harness = harness
    monkeypatch.setitem(sys.modules, "fake", package)
    monkeypatch.setitem(sys.modules, "fake.harness", harness)

    model = AppModel(app_root=APP, framework="django", endpoints=[
        Endpoint(func="views.edit", route="/bookmarks/<int:bookmark_id>/edit",
                 methods=["GET"], params=[Param(name="bookmark_id", location="path")],
                 preconditions=[Precondition(var="Session.authed_as", op="truthy",
                                             file="views/views.py", line=4)],
                 effects=[], file="views/views.py", line=10),
        Endpoint(func="django.contrib.auth.LoginView", route="/login/",
                 methods=["POST"],
                 params=[Param(name="username", location="form"),
                         Param(name="password", location="form")],
                 effects=[Effect(var="Session.authed_as", op="set", key="username",
                                 file="views/urls.py", line=1)],
                 file="views/urls.py", line=1),
    ])
    monkeypatch.setattr(runner_module, "django_auth_completion",
                        lambda m, root: model)
    monkeypatch.setattr(runner_module, "app_goal_constraints",
                        lambda m, i, b: [p for p in m.endpoints[0].preconditions])

    from stateforge.state.planning.backward import plan_backward

    def fake_plan(model_, stg, spec):
        return PlanSegment(goal="auth", steps=[
            PlanStep(action="POST /login/", form={"username": "x", "password": "x"}),
            PlanStep(action="GET bookmarks/<int:bookmark_id>/edit")])

    monkeypatch.setattr(runner_module, "plan_app_chain",
                        lambda m, i, b: ([model.endpoints[0].preconditions[0]], fake_plan(m, None, None)))

    def fake_execute(segment, client, ir_, target_block, root, credentials=None,
                     trace_root=None, auth_probe=None):
        steps = []
        authenticated = False
        for step in segment.steps:
            method, _, route = step.action.partition(" ")
            path = route if route.startswith("/") else "/" + route
            if "<int:bookmark_id>" in path:
                path = path.replace("<int:bookmark_id>", "1")
            body = dict(step.form or {})
            if "/login" in path:
                # Credentials are environment knowledge; the executor injects
                # them here, exactly as the real one does.
                body.update(credentials or {})
                authenticated = app.login_works
                status = 302 if authenticated else 401
            else:
                hit = authenticated or app.edit_open
                status = 200 if hit else 302
                authenticated = authenticated and status == 200
            reached = (path.startswith("/bookmarks/") and status == 200)
            steps.append(SimpleNamespace(
                action=step.action, method=method, path=path, status=status,
                ok=(200 <= status < 400 and (status != 401)),
                target_covered=reached, session_authenticated=authenticated,
                blocking=None if status < 400 else f"http {status}",
                request={"method": method, "path": path,
                         **({"form": body} if body else {})}))
        covered = any(s.target_covered for s in steps)
        return SimpleNamespace(steps=steps, target_covered=covered,
                               verified=False, notes=[])

    monkeypatch.setattr(runner_module, "execute_app_chain", fake_execute)

    def fake_replay(checkpoint, ir_path, out, *, python=None, harness, app_root,
                    trace_root=None, credentials=None, mode="chain", timeout=300):
        evidence = {"verified": True, "pid": 999999, "parent_pid": 1,
                    "cold_start": True, "mode": mode,
                    "verification": {"verified": True, "complete": True,
                                     "target_steps": [1], "blocking": [],
                                     "notes": []},
                    "execution": {"steps": [], "target_covered": True,
                                  "verified": True, "notes": []}}
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(evidence), encoding="utf-8")
        return evidence

    monkeypatch.setattr(runner_module, "cold_replay_http", fake_replay)
    return model


def test_app_runner_verifies_and_saves_http_checkpoint(tmp_path, app_root, monkeypatch):
    ir = _build_ir(tmp_path)
    monkeypatch.setattr(runner_module, "build_icfg", lambda scope: ir)
    monkeypatch.setattr(runner_module, "extract_state_facts",
                        lambda scope, ir=None: [])
    _install_fake_client(monkeypatch, FakeApp(), ir)
    block = next(iter(ir.blocks))
    report = AppGoalRunner().run(app_root, block, analysis_root=app_root / "views",
                                 harness="fake.harness", out=tmp_path / "out").__dict__
    summary = report["summary"]
    assert summary["status"] == "verified", summary
    assert summary["prefix_necessary"] is True
    checkpoint = GoalCheckpoint.load(summary["checkpoint"])
    assert checkpoint.transport == "http"
    assert [s["path"] for s in checkpoint.steps] == ["/login/", "/bookmarks/1/edit"]
    assert checkpoint.steps[0]["form"] == {"username": "probe",
                                           "password": "Passw0rd!"}
    assert (tmp_path / "out" / "execution.json").is_file()


def test_login_that_does_not_establish_session_is_a_failure(tmp_path, app_root, monkeypatch):
    ir = _build_ir(tmp_path)
    monkeypatch.setattr(runner_module, "build_icfg", lambda scope: ir)
    monkeypatch.setattr(runner_module, "extract_state_facts",
                        lambda scope, ir=None: [])
    _install_fake_client(monkeypatch, FakeApp(login_works=False), ir)
    report = AppGoalRunner().run(app_root, next(iter(ir.blocks)),
                                 analysis_root=app_root / "views",
                                 harness="fake.harness", out=tmp_path / "out")
    assert not report.success
    assert report.summary["failed_stage"] == "verification"
    assert report.summary["checkpoint"] is None or "checkpoint" not in report.summary


def test_target_reachable_without_prefix_is_not_state_evidence(tmp_path, app_root, monkeypatch):
    ir = _build_ir(tmp_path)
    monkeypatch.setattr(runner_module, "build_icfg", lambda scope: ir)
    monkeypatch.setattr(runner_module, "extract_state_facts",
                        lambda scope, ir=None: [])
    _install_fake_client(monkeypatch, FakeApp(edit_open=True), ir)
    report = AppGoalRunner().run(app_root, next(iter(ir.blocks)),
                                 analysis_root=app_root / "views",
                                 harness="fake.harness", out=tmp_path / "out")
    assert not report.success
    assert report.summary["failed_stage"] == "isolation"
    assert report.summary["prefix_necessary"] is False
    assert report.summary["execution_verified"] is True


def test_plan_mode_does_not_execute(tmp_path, app_root, monkeypatch):
    ir = _build_ir(tmp_path)
    monkeypatch.setattr(runner_module, "build_icfg", lambda scope: ir)
    monkeypatch.setattr(runner_module, "extract_state_facts",
                        lambda scope, ir=None: [])
    _install_fake_client(monkeypatch, FakeApp(), ir)

    def boom(*a, **kw):
        raise AssertionError("plan mode must not execute")

    monkeypatch.setattr(runner_module, "execute_app_chain", boom)
    report = AppGoalRunner().run(app_root, next(iter(ir.blocks)),
                                 analysis_root=app_root / "views",
                                 harness="fake.harness", mode="plan",
                                 out=tmp_path / "out")
    assert report.success and report.summary["status"] == "planned"
    assert report.summary["verified"] is False


def test_missing_harness_or_adapter_is_reported_before_execution(tmp_path, app_root):
    ir = _build_ir(tmp_path)
    report = AppGoalRunner().run(app_root, "unknown", harness=None)
    assert report.summary["failed_stage"] == "validation"
    assert "--harness" in report.summary["error"]["message"] or \
        "unknown target block" in report.summary["error"]["message"]
    report = AppGoalRunner().run(app_root, "unknown", harness="fake.harness",
                                 mode="fuzz", out=tmp_path)
    assert report.summary["failed_stage"] == "analysis"


def test_fuzz_mode_requires_an_in_process_adapter(tmp_path, app_root, monkeypatch):
    ir = _build_ir(tmp_path)
    monkeypatch.setattr(runner_module, "build_icfg", lambda scope: ir)
    monkeypatch.setattr(runner_module, "extract_state_facts",
                        lambda scope, ir=None: [])
    _install_fake_client(monkeypatch, FakeApp(), ir)
    report = AppGoalRunner().run(app_root, next(iter(ir.blocks)),
                                 analysis_root=app_root / "views",
                                 harness="fake.harness", mode="fuzz",
                                 out=tmp_path / "out", fuzz_seconds=1,
                                 fuzz_runs=1)
    assert report.summary["failed_stage"] == "fuzzing"
    assert "--adapter" in report.summary["error"]["message"]
    # The verified checkpoint survives the fuzz-configuration failure.
    assert report.summary["execution_verified"] is True
    assert Path(report.summary["checkpoint"]).is_file()


def test_http_checkpoint_is_not_a_micro_target_checkpoint(tmp_path):
    checkpoint = GoalCheckpoint(
        checkpoint_id="gc-x", target_block=BLOCK, source_file=str(APP),
        goal_constraints=[], steps=[{"method": "GET", "path": "/bookmarks/1/edit"}],
        transport="http")
    segment = checkpoint.to_segment()
    assert segment.steps[0].action == "GET /bookmarks/1/edit"
    micro = GoalCheckpoint(checkpoint_id="gc-y", target_block=BLOCK,
                           source_file=str(APP), goal_constraints=[],
                           steps=[{"json": {"op": "ship_order"}}])
    assert micro.to_segment().steps[0].action == "POST op=ship_order"
