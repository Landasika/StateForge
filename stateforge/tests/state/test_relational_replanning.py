"""test_relational_replanning.py —— PR-06:失败记忆/计划去重/修复建议。

调度约束验证(任务书):
- 403 不直接等于归属错误——归因只认观测反证;
- 同键 (goal, baseline, signature, binding) 去重,新证据/换绑定才重试;
- 未知(不可观测/观测未定)不触发无限重规划;
- 有效链(VERIFIED_ACHIEVED)→ 可重放检查点;候选/失败链不升级。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from stateforge.solving.plan_ledger import (
    ACHIEVED,
    NOT_ACHIEVED,
    UNKNOWN,
    PlanAttempt,
    PlanLedger,
    attempt_from_run,
    attribute_failure,
    checkpoint_from_verified,
    suggest_repair,
)
from stateforge.solving.relational_planner import RelationalPlanCandidate

VULNSHOP = Path(__file__).parents[2] / "targets" / "vulnshop"
sys.path.insert(0, str(Path(__file__).parents[1] / "targets" / "vulnshop"))


def _eval(relations="UNKNOWN", auth="UNKNOWN", failed=None, status=UNKNOWN,
          reasons=(), witness=None):
    return SimpleNamespace(
        relations_truth=relations, auth_binding_truth=auth,
        failed_constraint=failed, achievement_status=status,
        unknown_reasons=tuple(reasons), evidence_refs=("ev@pt",),
        observation_point="pt-1", witness=witness)


def _cand(signature="sig0001"):
    return RelationalPlanCandidate(
        goal_id="g-ship", steps=({"method": "POST", "path": "/x"},),
        signature=signature)


def _record(status=403, error=None):
    step = SimpleNamespace(status=status, error=error)
    return SimpleNamespace(steps=[step])


# ------------------------------------------------------------- 归因

def test_attribution_ignores_http_status_alone():
    """403 + 判定 UNKNOWN → unknown-pending(状态码永不单独归因)。"""
    assert attribute_failure(_eval(), http_status=403) \
        == "unknown-pending-observation"


def test_attribution_auth_mismatch_needs_observation():
    """403 + 观测反证(auth FALSE:探测主体≠绑定)→ auth-subject-mismatch;
    同样 403 但关系反证在 user_id → ownership(归因由证据决定,非状态码)。"""
    assert attribute_failure(_eval(auth="FALSE"), 403) \
        == "auth-subject-mismatch"
    assert attribute_failure(
        _eval(relations="FALSE", failed="EQ(?order.user_id, ?user.id) @ {}",
              status=NOT_ACHIEVED), 403) == "identity-ownership-refuted"


def test_attribution_by_refuted_predicate():
    assert attribute_failure(
        _eval(relations="FALSE", failed="EQ(?order.status, 'PAID') @ {}",
              status=NOT_ACHIEVED), 409) == "state-precondition-refuted"
    assert attribute_failure(
        _eval(relations="FALSE", failed="EXISTS(?order) @ {}",
              status=NOT_ACHIEVED), 404) == "resource-absent-refuted"
    assert attribute_failure(
        _eval(relations="FALSE", failed="EQ(?o.k, 1) @ {}",
              status=NOT_ACHIEVED), 400) == "relation-refuted"


def test_repair_suggestions_and_unknown_pending():
    assert len(suggest_repair("auth-subject-mismatch")) == 2
    assert suggest_repair("unknown-pending-observation") == []  # 待观察
    assert any("extract" in s for s in suggest_repair(
        "resource-absent-refuted"))


# ------------------------------------------------------------- 去重

def _ledger_with(attempt):
    led = PlanLedger()
    led.record(attempt)
    return led


def test_dedup_rules():
    cand = _cand()
    key = dict(goal_id="g-ship", baseline_id="b1",
               plan_signature="sig0001", binding={"?user": "bob"})
    mk = lambda st, failed=None: PlanAttempt(
        attempt_id="a", goal_id="g-ship", baseline_id="b1",
        plan_signature="sig0001", binding={"?user": "bob"},
        achievement_status=st, failed_predicate=failed)

    led = PlanLedger()
    ok, why = led.should_retry(**key)
    assert ok and "no-prior" in why

    led.record(mk(NOT_ACHIEVED, "EQ(?order.user_id, ?user.id)"))
    ok, why = led.should_retry(**key)
    assert not ok and "failed-dedup" in why
    ok, why = led.should_retry(**key, new_evidence=True)
    assert ok and "new-evidence" in why

    ok, why = led.should_retry(**{**key, "binding": {"?user": "alice"}})
    assert ok                                   # 换绑定 → 新键
    ok, why = led.should_retry(**{**key, "baseline_id": "b2"})
    assert ok                                   # 换基线 → 新键


def test_unknown_does_not_loop_but_allows_new_evidence():
    key = dict(goal_id="g", baseline_id="b", plan_signature="s",
               binding={})
    led = _ledger_with(PlanAttempt(
        attempt_id="u", goal_id="g", baseline_id="b", plan_signature="s",
        binding={}, achievement_status=UNKNOWN,
        unknown_reasons=("auth-association-unverified",)))
    ok, why = led.should_retry(**key)
    assert not ok and "unknown-pending" in why  # 不触发无限重规划
    ok, why = led.should_retry(**key, new_evidence=True)
    assert ok


def test_achieved_closes_goal():
    key = dict(goal_id="g", baseline_id="b", plan_signature="s",
               binding={})
    led = _ledger_with(PlanAttempt(
        attempt_id="v", goal_id="g", baseline_id="b", plan_signature="s",
        binding={}, achievement_status=ACHIEVED))
    ok, why = led.should_retry(**key, new_evidence=True)
    assert not ok and "achieved" in why         # 新证据也不能重开已达成


def test_ledger_jsonl_roundtrip(tmp_path):
    path = tmp_path / "attempts.jsonl"
    led = PlanLedger(path)
    cand = _cand()
    att = attempt_from_run(cand, _record(status=403),
                           _eval(auth="FALSE", status=NOT_ACHIEVED,
                                 failed="auth"),
                           baseline_id="cold-1",
                           binding={"?user": "bob", "?order": "orders:1"},
                           reached_blocks=12, trace_target_covered=False)
    led.record(att)
    led2 = PlanLedger(path)
    assert len(led2.attempts) == 1
    got = led2.attempts[0]
    assert got.failure_reason == "auth-subject-mismatch"
    assert got.http_status == 403 and got.reached_blocks == 12
    assert got.binding == {"?user": "bob", "?order": "orders:1"}
    assert led2.failed_constraints("g-ship") == ["auth"]


# ------------------------------------------------------------- 检查点升级

def test_checkpoint_only_from_verified():
    cand = _cand()
    verified = SimpleNamespace(candidate=cand, achievement_status=UNKNOWN,
                               witness=None)
    with pytest.raises(ValueError, match="VERIFIED_ACHIEVED"):
        checkpoint_from_verified(verified, "app.py")
    ok = SimpleNamespace(candidate=cand, achievement_status=ACHIEVED,
                         witness={"binding": {}})
    ckpt = checkpoint_from_verified(ok, "app.py")
    assert ckpt.checkpoint_id == "vp-sig0001"
    assert ckpt.steps == [dict(s) for s in cand.steps]


# ------------------------------------------------------------- E2E 闭环

def test_wrong_user_then_repair_rebind_succeeds(tmp_path):
    """错误用户发货 → 403 → 观测归因 auth-mismatch → 同键去重拒绝盲目重试
    → 修复建议换绑定 → alice 名下整链重试 → VERIFIED_ACHIEVED → 检查点。"""
    from stateforge.analysis.static.icfg import build_icfg
    from stateforge.solving.relational_planner import plan_for_relational_goal
    from stateforge.solving.vulnshop_state_bridge import appmodel_path
    from stateforge.state.goal_derivation import derive_relational_goal
    from stateforge.state.goal_evaluation import classify_achievement
    from stateforge.targets.vulnshop.fuzz_target import make_instance
    from test_relational_goal_runtime import _run_chain, _view
    from test_relational_plan_e2e import _four_component, _env_for

    ir = build_icfg(VULNSHOP)
    target = next(b.id for b in ir.blocks.values()
                  if b.function == "ship_order" and b.id.startswith("app.py")
                  and any("UPDATE orders SET status='SHIPPED'" in s
                          for s in b.statements))
    goal = derive_relational_goal(ir, target)
    cand = plan_for_relational_goal(
        goal, appmodel_path(VULNSHOP / "fuzz_target.py"), salt="rep")
    ledger = PlanLedger(tmp_path / "attempts.jsonl")

    # ① bob 完整链(停在 pay)+ alice 登录后 ship → 403 + auth 反证
    steps = list(cand.steps)
    wrong = steps[:4] + [
        {"method": "POST", "path": "/auth/register",
         "json": {"username": "alice", "password": "pw"}},
        {"method": "POST", "path": "/auth/login",
         "json": {"username": "alice", "password": "pw"},
         "extract": {"token": "?token"}},
        steps[4]]
    inst = make_instance(tmp_path / "wrong.db", guards=1)
    record, session = _run_chain(inst, wrong, ir=ir)
    assert record.steps[-1].status == 403
    pre, trace_hit = _four_component(record, goal, session, ir, target, wrong)
    assert pre.auth_binding_truth == "FALSE"

    failed_binding = {"?user": "sf_u_rep",
                      "?order": str(record.steps[2].bindings["oid"])}
    attempt = attempt_from_run(
        cand, record, pre, baseline_id="cold-1",
        binding=failed_binding, trace_target_covered=trace_hit)
    ledger.record(attempt)
    assert attempt.failure_reason == "auth-subject-mismatch"
    assert attempt.http_status == 403

    # ② 同键去重(同一 goal/baseline/signature/binding):不无条件重复同一链
    ok, why = ledger.should_retry(
        goal_id=cand.goal_id, baseline_id="cold-1",
        plan_signature=cand.signature, binding=failed_binding)
    assert not ok and "failed-dedup" in why
    assert suggest_repair(attempt.failure_reason)  # 修复建议在账

    # ③ 修复落地:换绑定——整链以 alice 身份执行(用户名替换)
    alice_chain = json.loads(json.dumps(list(cand.steps)))
    for s in alice_chain[:2]:
        s["json"]["username"] = "alice"
    ok2, _ = ledger.should_retry(
        goal_id=cand.goal_id, baseline_id="cold-1",
        plan_signature=cand.signature, binding={"?user": "alice"})
    assert ok2                                     # 新绑定 → 允许重试

    inst2 = make_instance(tmp_path / "fixed.db", guards=1)
    record2, session2 = _run_chain(inst2, alice_chain, ir=ir)
    assert [s.status for s in record2.steps] == [201, 200, 201, 200, 200]
    pre2, trace2 = _four_component(record2, goal, session2, ir, target,
                                   alice_chain)
    status2 = classify_achievement(
        relations_truth=pre2.relations_truth,
        path_conditions_truth=pre2.path_conditions_truth,
        auth_binding_truth=pre2.auth_binding_truth,
        target_trace_covered="TRUE" if trace2 else "FALSE",
        goal_complete=not goal.unresolvable)
    assert status2 == ACHIEVED

    # ④ 有效链 → 检查点;失败链此前已证明不升级
    verified = SimpleNamespace(candidate=cand, achievement_status=status2,
                               witness=pre2.witness.to_dict()
                               if pre2.witness else None)
    ckpt = checkpoint_from_verified(verified, str(VULNSHOP / "app.py"))
    assert ckpt.sequence_seed()  # 符号链可重放(extract 重建 ID)
