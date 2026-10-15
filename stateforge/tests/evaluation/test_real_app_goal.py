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
