"""test_relational_plan_e2e.py —— PR-05/M0-C 端到端:冷启动自动到达+重放。

四验收案例(任务书 M0-C):
1. GUARDS=1 空基线自动生成有效链(非手写五步):发货前联判真、发货后
   SHIPPED、Trace 命中 UPDATE;
2. 注入切换用户动作:目标关系失败,不得标 verified;
3. 清空数据后完整重放再次成功(资源 ID 由 extract 重建);
4. GUARDS=0 路径不被错误套上 guards=True 前置(链无 pay 且执行成功)。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stateforge.analysis.static.icfg import build_icfg
from stateforge.runtime.mapper import map_trace
from stateforge.solving.relational_planner import (
    VerifiedPlan,
    detect_auth_rebinding,
    plan_for_relational_goal,
)
from stateforge.solving.vulnshop_state_bridge import appmodel_path
from stateforge.state.goal_derivation import derive_relational_goal
from stateforge.state.goal_evaluation import (
    VERIFIED_ACHIEVED,
    VERIFIED_NOT_ACHIEVED,
    ObservationView,
    classify_achievement,
    evaluate_goal,
)
from stateforge.state.relational import Const, Field, RelOp, RelPredicate
from stateforge.targets.vulnshop.fuzz_target import make_instance

# 复用 M0-B 的执行/探测/绑定机制(PRG-04 交付)
import sys
sys.path.insert(0, str(Path(__file__).parent))
from test_relational_goal_runtime import (  # noqa: E402
    FAMILIES,
    _run_chain,
    _view,
)

VULNSHOP = Path(__file__).parents[3] / "targets" / "vulnshop"


@pytest.fixture(scope="module")
def goal_ir():
    ir = build_icfg(VULNSHOP)
    target = next(b.id for b in ir.blocks.values()
                  if b.function == "ship_order" and b.id.startswith("app.py")
                  and any("UPDATE orders SET status='SHIPPED'" in s
                          for s in b.statements))
    goal = derive_relational_goal(ir, target)
    return goal, ir, target


@pytest.fixture(scope="module")
def candidate(goal_ir):
    goal, _, _ = goal_ir
    amp = appmodel_path(VULNSHOP / "fuzz_target.py")
    return plan_for_relational_goal(goal, amp, salt="e2e")


def _user_of(steps) -> str:
    return steps[0]["json"]["username"]


def _env_for(record, steps):
    user = _user_of(steps)
    oid = str(record.steps[2].bindings["oid"])
    from stateforge.state.goal_evaluation import BindingEnv
    return BindingEnv(fixed={"?order": [oid], "?user": [user]},
                      provenance={"?order": "extract:?oid",
                                  "?user": "auth:token"})




def _four_component(record, goal, session, ir, target, steps):
    """trace 先独立判定,再作为第四分量传入 evaluate_goal(四分量 API
    的既定用法;全 TRUE 时 witness 由 evaluate_goal 产生)。"""
    a, b = record.steps[-1].trace_span
    covered = set(map_trace(session.events[a:b], ir).blocks)
    trace_hit = target in covered
    pre = evaluate_goal(goal, _view(record.steps[-1].before, None,
                                    "before-ship"),
                        _env_for(record, steps),
                        path_context={"guards": True, "err": False},
                        trace_covered=trace_hit)
    return pre, trace_hit


# ------------------------------------------------- 案例 1:自动链冷启动判达

def test_case1_auto_chain_achieves_goal(tmp_path, goal_ir, candidate):
    """空基线自动链(规划器产出,非手写):四分量 VERIFIED_ACHIEVED。"""
    goal, ir, target = goal_ir
    assert candidate is not None and len(candidate.steps) == 5
    inst = make_instance(tmp_path / "c1.db", guards=1)
    record, session = _run_chain(inst, list(candidate.steps), ir=ir)
    assert [s.status for s in record.steps] == [201, 200, 201, 200, 200]

    pre, trace_hit = _four_component(record, goal, session, ir, target,
                                     candidate.steps)
    assert pre.relations_truth == "TRUE"
    assert pre.auth_binding_truth == "TRUE"
    assert trace_hit                                # Trace 独立命中 UPDATE

    # 终态:同一 ?oid 的 SHIPPED
    post_rel = RelPredicate(RelOp.EQ, Field("?order", "status"),
                            Const("SHIPPED"), "Eq", "app.py", 275)
    post = evaluate_goal(
        type("G", (), {"relations": (post_rel,),
                       "var_provenance": goal.var_provenance,
                       "target_block": goal.target_block,
                       "path_conditions": (), "unresolvable": ()})(),
        _view(record.steps[-1].after, None, "after-ship"),
        _env_for(record, candidate.steps),
        path_context={"guards": True, "err": False}, trace_covered=None)
    assert post.relations_truth == "TRUE"

    status = classify_achievement(
        relations_truth=pre.relations_truth,
        path_conditions_truth=pre.path_conditions_truth,
        auth_binding_truth=pre.auth_binding_truth,
        target_trace_covered="TRUE" if trace_hit else "FALSE",
        goal_complete=not goal.unresolvable)
    assert status == VERIFIED_ACHIEVED

    verified = VerifiedPlan(
        candidate=candidate, achievement_status=status,
        relations_truth=pre.relations_truth,
        auth_binding_truth=pre.auth_binding_truth,
        trace_covered=trace_hit,
        witness=pre.witness.to_dict() if pre.witness else None)
    assert verified.witness is not None
    assert verified.candidate.signature == candidate.signature


# ------------------------------------------------- 案例 2:身份切换不通过

def test_case2_user_switch_not_verified(tmp_path, goal_ir, candidate):
    """注入 alice 注册+登录(重绑定 ?token)后 ship:403 + auth FALSE +
    归属反证 → VERIFIED_NOT_ACHIEVED;重绑定检测亦标注。"""
    goal, ir, target = goal_ir
    steps = list(candidate.steps)
    switched = steps[:4] + [
        {"method": "POST", "path": "/auth/register",
         "json": {"username": "alice", "password": "pw"}},
        {"method": "POST", "path": "/auth/login",
         "json": {"username": "alice", "password": "pw"},
         "extract": {"token": "?token"}},
        steps[4]]
    assert detect_auth_rebinding(switched)          # 规划层失效标注

    inst = make_instance(tmp_path / "c2.db", guards=1)
    record, session = _run_chain(inst, switched, ir=ir)
    assert record.steps[-1].status == 403

    pre, trace_hit = _four_component(record, goal, session, ir, target,
                                     switched)
    assert pre.auth_binding_truth == "FALSE"        # 探测 alice ≠ ?user
    assert pre.relations_truth == "TRUE"            # bob 的单确实 PAID+属 bob
    status = classify_achievement(
        relations_truth=pre.relations_truth,
        path_conditions_truth=pre.path_conditions_truth,
        auth_binding_truth=pre.auth_binding_truth,
        target_trace_covered="TRUE", goal_complete=True)
    assert status == VERIFIED_NOT_ACHIEVED          # 不得标 verified


# ------------------------------------------------- 案例 3:冷启动重放

def test_case3_cold_replay_succeeds(tmp_path, goal_ir, candidate):
    """两次独立空基线重放同一(符号)链:均全量成功;资源 ID 由 extract
    重建;检查点(C10 序列种子)与原链逐字节一致。"""
    goal, ir, target = goal_ir
    from stateforge.fuzzing.entry_driver import encode_sequence_seed
    from stateforge.solving.goal_checkpoint import GoalCheckpoint

    seed_bytes = encode_sequence_seed(list(candidate.steps))
    ckpt = GoalCheckpoint(
        checkpoint_id="mc-" + candidate.signature,
        target_block=goal.target_block,
        source_file=str(VULNSHOP / "app.py"),
        goal_constraints=[], steps=list(candidate.steps))
    assert ckpt.sequence_seed() == seed_bytes       # 重放规则=符号链

    for i in range(2):
        inst = make_instance(tmp_path / f"c3-{i}.db", guards=1)
        record, session = _run_chain(inst, list(candidate.steps), ir=ir)
        assert [s.status for s in record.steps] == [201, 200, 201, 200, 200]
        pre, trace_hit = _four_component(record, goal, session, ir, target,
                                         candidate.steps)
        assert pre.relations_truth == "TRUE" and pre.auth_binding_truth == "TRUE"
        assert trace_hit
        assert record.steps[2].bindings.get("oid") is not None  # extract 重建


# ------------------------------------------------- 案例 4:guards=0 不套前置

def test_case4_guards0_chain_executes_without_pay(tmp_path, goal_ir):
    """guards=0:候选链无 pay;空基线执行 ship 直接成功(guards=True 的
    PAID/归属前置未被错误套上)。"""
    goal, _, _ = goal_ir
    amp = appmodel_path(VULNSHOP / "fuzz_target.py")
    c0 = plan_for_relational_goal(goal, amp, salt="e2e",
                                  runtime_path_context={"guards": False})
    paths = [s["path"] for s in c0.steps]
    assert "/orders/?oid/pay" not in paths          # pay 被豁免
    assert paths[-1] == "/orders/?oid/ship"

    inst = make_instance(tmp_path / "c4.db", guards=0)
    record, session = _run_chain(inst, list(c0.steps), ir=goal_ir[1])
    assert [s.status for s in record.steps] == [201, 200, 201, 200]  # ship 200
    a, b = record.steps[-1].trace_span
    from stateforge.runtime.mapper import map_trace as _mt
    covered = set(_mt(session.events[a:b], goal_ir[1]).blocks)
    assert goal_ir[2] in covered                    # UPDATE 仍被执行
