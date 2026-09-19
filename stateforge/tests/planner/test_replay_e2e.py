"""SF3-T5 直跑 e2e:ANOTA 子进程服务 vulnshop,驱动侧 规划→重放→判定。

5 goal × {vuln, fixed} 十格矩阵:
  vuln(GUARDS=0)  全部 hit(状态构造主链证明)
  fixed(GUARDS=1) 全部 blocked/missed(守卫分化正确)
"""

import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from stateforge.analyzer.analyzer import analyze
from stateforge.executor.replay import GoalResult, ReplayExecutor
from stateforge.graph.builder import build_sstg
from stateforge.planner.backward import plan_backward
from stateforge.planner.checkers import CHECKERS
from stateforge.planner.goals import VULNSHOP_GOALS

VULNSHOP = Path(__file__).parents[2] / "targets" / "vulnshop"
ANOTA_PY = Path("/home/landasika/icassp/anota/ANOTA/ANOTA-interpreter/python")


@pytest.fixture(scope="module")
def sstg_model():
    m = analyze(VULNSHOP, "flask")
    return m, build_sstg(m)


def _serve(guards: int, port: int, db: Path):
    proc = subprocess.Popen(
        [str(ANOTA_PY), str(VULNSHOP / "serve.py"), str(guards), str(port), str(db)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/", timeout=1).status_code == 200:
                return proc
        except httpx.HTTPError:
            time.sleep(0.25)
    proc.kill()
    raise RuntimeError(f"vulnshop guards={guards} 未在 15s 内就绪")


# (goal, guards, 期望 hit, 期望形态)
EXPECT = [
    ("goal_workflow_bypass", 0, True, "hit"),
    ("goal_workflow_bypass", 1, False, "blocked"),
    ("goal_coupon_reuse", 0, True, "hit"),
    ("goal_coupon_reuse", 1, False, "blocked"),
    ("goal_idor", 0, True, "hit"),
    ("goal_idor", 1, False, "blocked"),
    ("goal_payment_bypass", 0, True, "hit"),
    ("goal_payment_bypass", 1, False, "missed"),  # 链走通但状态不达标(部分支付不 PAID)
    ("goal_privilege", 0, True, "hit"),
    ("goal_privilege", 1, False, "blocked"),
]


def run_goal(model, graph, goal, guards, port, tmp_db):
    goal_obj = next(g for g in VULNSHOP_GOALS if g.name == goal)
    seg = plan_backward(model, graph, goal_obj)
    ex = ReplayExecutor(base_url=f"http://127.0.0.1:{port}")
    return ex.run_segment(seg, goal_obj, checker=CHECKERS[goal])


@pytest.mark.parametrize("goal,guards,want_hit,shape", EXPECT)
def test_goal_outcome(goal, guards, want_hit, shape, sstg_model, tmp_path):
    m, g = sstg_model
    port = 5040 + guards
    proc = _serve(guards, port, tmp_path / f"shop-g{guards}.db")
    try:
        result = run_goal(m, g, goal, guards, port, tmp_path)
        assert result.hit == want_hit, (
            f"{goal} guards={guards}: hit={result.hit} reason={result.reason} "
            f"steps={[(t.route, t.status) for t in result.steps]}"
        )
        if shape == "blocked":
            assert "blocked at" in result.reason
        elif shape == "missed":
            assert "不成立" in result.reason
        elif shape == "hit":
            assert result.reason
    finally:
        proc.terminate()
        proc.wait(timeout=5)
