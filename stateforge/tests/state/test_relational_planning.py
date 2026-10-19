"""test_relational_planning.py —— PR-05/M0-C 单元级:关系合取 → 计划候选。

不执行 HTTP;链构造/槽位同一性/义务/失效标注/guards 条件化的机器面。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stateforge.analysis.static.icfg import build_icfg
from stateforge.solving.relational_planner import (
    RelationalPlanCandidate,
    detect_auth_rebinding,
    plan_for_relational_goal,
)
from stateforge.solving.vulnshop_state_bridge import appmodel_path
from stateforge.state.goal_derivation import derive_relational_goal

VULNSHOP = Path(__file__).parents[2] / "targets" / "vulnshop"


@pytest.fixture(scope="module")
def planned():
    ir = build_icfg(VULNSHOP)
    target = next(b.id for b in ir.blocks.values()
                  if b.function == "ship_order" and b.id.startswith("app.py")
                  and any("UPDATE orders SET status='SHIPPED'" in s
                          for s in b.statements))
    goal = derive_relational_goal(ir, target)
    amp = appmodel_path(VULNSHOP / "fuzz_target.py")
    return goal, amp, plan_for_relational_goal(goal, amp, salt="unit")


def _paths(candidate) -> list[str]:
    return [str(s.get("path")) for s in candidate.steps]


def test_guards1_chain_is_full_five_step(planned):
    """完整关系合取 → register→login→create→pay→ship(自动生成,
    非手写五步;含 pay=状态关系链,ship 钉尾)。"""
    goal, amp, cand = planned
    assert cand is not None
    assert _paths(cand) == ["/auth/register", "/auth/login", "/orders",
                            "/orders/?oid/pay", "/orders/?oid/ship"]
    assert not cand.uncovered_relations
    assert cand.conditional_on == ("guards",)       # 生效依赖旗标


def test_full_conjunction_not_single_atom(planned):
    """区别于 _goal_atom 单原子:状态关系成链 + 归属关系入义务 +
    EXISTS 由资源创建覆盖——三者都在候选中留痕。"""
    _, _, cand = planned
    obl_notes = " ".join(o.note for o in cand.obligations)
    assert "?token" in obl_notes and "同一 token" in obl_notes
    assert any(o.slot == "?oid" for o in cand.obligations)
    assert any(o.slot == "relation" and "user_id" in o.note
               for o in cand.obligations)            # 归属关系显式留痕


def test_slot_unification(planned):
    """符号同一性:?token 单一生产者(login)×三消费者;
    ?oid 由 create extract 产生、pay/ship 路径消费;无写死 ID。"""
    _, _, cand = planned
    tok = next(o for o in cand.obligations if o.slot == "?token")
    assert len(tok.producers) == 1 and "/auth/login" in tok.producers[0]
    assert len(tok.consumers) == 3
    oid = next(o for o in cand.obligations if o.slot == "?oid")
    assert any(p.endswith("POST /orders") for p in oid.producers)
    assert len(oid.consumers) == 2
    assert all("?oid" in p for p in _paths(cand)[3:])
    assert "1" not in _paths(cand)                   # 资源 ID 不写死


def test_guards0_drops_guarded_preconditions(planned):
    """guards=False:守卫条件化的关系被豁免(不套 guards=1 前置),
    链无 pay;豁免关系逐条留痕。"""
    goal, amp, _ = planned
    c0 = plan_for_relational_goal(goal, amp, salt="unit",
                                  runtime_path_context={"guards": False})
    assert _paths(c0) == ["/auth/register", "/auth/login", "/orders",
                          "/orders/?oid/ship"]
    dropped = " ".join(c0.dropped_relations)
    assert "status" in dropped and "user_id" in dropped
    assert c0.conditional_on == ()


def test_signature_deterministic_and_content_sensitive(planned):
    goal, amp, cand = planned
    again = plan_for_relational_goal(goal, amp, salt="unit")
    assert again.signature == cand.signature         # 同输入同签名
    c0 = plan_for_relational_goal(goal, amp, salt="unit",
                                  runtime_path_context={"guards": False})
    assert c0.signature != cand.signature            # 不同链不同签名


def test_candidate_is_not_verified_type():
    """候选与验证产物是不同概念:类型分离,候选无判达字段。"""
    c = RelationalPlanCandidate(goal_id="g", steps=())
    assert not hasattr(c, "achievement_status")
    from stateforge.solving.relational_planner import VerifiedPlan
    v = VerifiedPlan(candidate=c, achievement_status="UNKNOWN",
                     relations_truth="UNKNOWN", auth_binding_truth="UNKNOWN",
                     trace_covered=False, witness=None)
    assert v.candidate is c and v.achievement_status == "UNKNOWN"


def test_detect_auth_rebinding_flags_double_login(planned):
    """失效语义:注入第二次 login → 重绑定标注(不拒绝,交验证裁决)。"""
    _, _, cand = planned
    assert detect_auth_rebinding(list(cand.steps)) == []
    switched = list(cand.steps[:4]) + [
        {"method": "POST", "path": "/auth/register",
         "json": {"username": "eve", "password": "pw"}},
        {"method": "POST", "path": "/auth/login",
         "json": {"username": "eve", "password": "pw"},
         "extract": {"token": "?token"}},
        cand.steps[4]]
    flags = detect_auth_rebinding(switched)
    assert flags and "auth-rebinding" in flags[0] and "2 次" in flags[0]


def test_goal_to_plan_legacy_unaffected():
    """旧微靶规划入口(goal_to_plan)不经本路径,回归不破。"""
    from stateforge.state.goal_to_plan import dispatch_arms
    src = VULNSHOP / "app.py"
    arms = dispatch_arms(str(src))
    assert isinstance(arms, list) or arms == []      # vulnshop 非 op 分派靶
