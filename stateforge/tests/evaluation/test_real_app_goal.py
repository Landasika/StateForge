"""真实应用端到端验收:linkding(生产级 Django)目标块判达。

两段式(静态在主环境,执行在应用 venv——依赖隔离的架构事实):
  阶段一 goal_app_bridge:analyze → Django 框架补全(@login_required →
  需 auth;内置 LoginView → 产 auth)→ 目标块约束 → plan_backward 链
  阶段二 http_plan_executor:harness test_client 真实 HTTP 执行,
  轨迹收窄到视图目录,块级判达

目标:edit 视图 GET 路径深块(含 access.bookmark_write 归属查询)。
诚实边界:POST 保存分支(form.save)需 Django 表单字段词汇,留后续;
ORM 内联归属谓词(owner=request.user)词面不可提,执行期块覆盖裁决。
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
LINKDING = REPO / "external" / "linkding"
VENV_PY = LINKDING / ".venv" / "bin" / "python"
TARGET = "bookmarks/views/bookmarks.py::edit::239-241::block::1"

pytestmark = pytest.mark.skipif(
    not VENV_PY.exists(), reason="linkding venv 未安装(接入门前置)")


@pytest.fixture(scope="module")
def plan_and_ir(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("linkding_goal")
    plan_json = tmp / "plan.json"
    ir_json = tmp / "ir.json"
    r = subprocess.run(
        [sys.executable, "-m", "stateforge.state.goal_app_bridge",
         str(LINKDING), "django", TARGET, "--out", str(plan_json)],
        cwd=REPO, capture_output=True, text=True, timeout=900)
    assert r.returncode == 0, r.stderr[-400:]
    from stateforge.analysis.static.icfg import build_icfg
    from stateforge.core.serialization import save_program_ir

    save_program_ir(build_icfg(LINKDING), str(ir_json))
    return plan_json, ir_json


def test_stage1_constraints_and_chain(plan_and_ir):
    plan_json, _ = plan_and_ir
    plan = json.loads(plan_json.read_text(encoding="utf-8"))
    assert plan["constraints"] == [
        {"var": "Session.authed_as", "op": "truthy", "value": None}]
    actions = [s["action"] for s in plan["segment"]["steps"]]
    assert actions == ["POST /login/", "GET bookmarks/<int:bookmark_id>/edit"]
    assert any("final-action" in n for n in plan["notes"])


def test_stage2_real_http_reaches_target_block(plan_and_ir, capsys):
    plan_json, ir_json = plan_and_ir
    r = subprocess.run(
        [str(VENV_PY), "-m", "stateforge.runtime.http_plan_executor",
         str(plan_json), "stateforge.targets.apps20.linkding.harness",
         "--ir", str(ir_json),
         "--trace-root", str(LINKDING / "bookmarks" / "views")],
        cwd=LINKDING, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr[-400:]
    assert "target_covered: True" in r.stdout
    assert "302" in r.stdout and "200" in r.stdout


# --------------------------------------------------------------- full pipeline
#
# The two-stage test above proves the executor can reach the block. This one
# proves the *pipeline* can: one command goes analysis → goal → plan → execute
# → isolation probe → checkpoint → cold replay → C10 seed → real Atheris, and
# every stage must leave evidence.

NARROW = LINKDING / "bookmarks" / "views"
NARROW_TARGET = "bookmarks.py::edit::239-241::block::1"
ADAPTER = (REPO / "stateforge" / "targets" / "real_apps" / "linkding"
           / "inproc_target.py")


def _app_goal_run(out: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(VENV_PY), "-m", "stateforge.cli", "app-goal-run", str(LINKDING),
         "--block", NARROW_TARGET, "--analysis-root", str(NARROW),
         "--harness", "stateforge.targets.apps20.linkding.harness",
         "--out", str(out), *extra],
        cwd=REPO, capture_output=True, text=True, timeout=1800)


def test_pipeline_executes_and_cold_replays(tmp_path):
    out = tmp_path / "run"
    r = _app_goal_run(out, "--mode", "execute")
    assert r.returncode == 0, (r.stdout[-1500:], r.stderr[-800:])
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "verified"
    assert all(v == "ok" for v in summary["stages"].values()), summary["stages"]
    assert summary["target_covered"] and summary["execution_verified"]
    # The prefix must be load-bearing, not merely sufficient.
    assert summary["prefix_necessary"] is True
    # Cold replay is a different process than the runner.
    replay = summary["cold_replay"]
    assert replay["verified"] and replay["pid"] != replay["parent_pid"]
    # The plan is the HTTP chain derived from the framework guard.
    plan = json.loads((out / "plan.json").read_text(encoding="utf-8"))
    assert [s["action"] for s in plan["steps"]] == [
        "POST /login/", "GET bookmarks/<int:bookmark_id>/edit"]
    goal = json.loads((out / "goal.json").read_text(encoding="utf-8"))
    assert goal["constraints"] == [
        {"var": "Session.authed_as", "op": "truthy", "value": None}]


def test_pipeline_seed_is_consumed_by_real_atheris(tmp_path):
    out = tmp_path / "fuzz"
    r = _app_goal_run(out, "--mode", "fuzz", "--adapter", str(ADAPTER),
                      "--fuzz-seconds", "2", "--fuzz-runs", "20")
    assert r.returncode == 0, (r.stdout[-1500:], r.stderr[-800:])
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    fuzzing = summary["fuzzing"]
    assert fuzzing["verified"] and fuzzing["consumed"]
    assert fuzzing["target_covered"] and fuzzing["engine"] == "atheris"
    assert fuzzing["sequence_steps"] == 2
    receipt = fuzzing["matching_receipts"][0]
    assert receipt["engine"] == "atheris"
    assert receipt["phase"] == "input_callback"
    assert receipt["completed_steps"] == 2
    assert receipt["lifecycle"] == "reset" and receipt["isolation"] == "reset"
    assert receipt["target_covered"] and receipt["error"] is None


def test_pipeline_replay_mode_reproduces_a_saved_checkpoint(tmp_path):
    first = tmp_path / "execute"
    assert _app_goal_run(first, "--mode", "execute").returncode == 0
    saved = json.loads((first / "summary.json").read_text(encoding="utf-8"))
    checkout = tmp_path / "replay"
    r = _app_goal_run(checkout, "--mode", "replay",
                      "--checkpoint", saved["checkpoint"])
    assert r.returncode == 0, (r.stdout[-1500:], r.stderr[-800:])
    summary = json.loads((checkout / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "verified"
    assert summary["input_checkpoint"] == saved["checkpoint"]
    assert summary["cold_replay"]["verified"]
