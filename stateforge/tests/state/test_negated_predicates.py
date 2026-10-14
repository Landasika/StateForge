"""否定谓词验收:方向感知推导 + 身份刷新步(shop 越权分支 94-95)。

false 边必要守卫的 EQ 反转为 NE(值侧符号引用 session.uid);
NE 约束触发链后处理——终结动作前插入 session 变量生产者的
刷新步(distinct 值),构造 owner≠uid 时序 → 越权分支判达。
"""

from pathlib import Path

from stateforge.analysis.static.icfg import build_icfg
from stateforge.runtime.plan_executor import (_load_target_module,
                                              execute_plan)
from stateforge.state.extractor import extract_state_facts
from stateforge.state.goal_derivation import (derive_state_goal,
                                              necessary_guard_edges)
from stateforge.state.goal_to_plan import dispatch_arms, plan_for_block

SHOP = Path(__file__).parents[2] / "targets" / "stateful"
SRC = str(SHOP / "t_state_shop.py")
PRIV = "t_state_shop.py::_handle::91-92::block::1"
DENY = "t_state_shop.py::_handle::94-95::block::1"


def _ir_facts():
    ir = build_icfg(SHOP)
    return ir, extract_state_facts(SHOP, ir=ir)


def test_guard_edges_carry_direction():
    ir, _ = _ir_facts()
    edges = dict((b, k) for b, k in necessary_guard_edges(ir, DENY))
    assert edges.get("t_state_shop.py::_handle::90-90::branch::1") == "false"
    edges_p = dict((b, k) for b, k in necessary_guard_edges(ir, PRIV))
    assert edges_p.get("t_state_shop.py::_handle::90-90::branch::1") == "true"


def test_deny_branch_derives_ne_with_symbolic_value():
    ir, facts = _ir_facts()
    goal = derive_state_goal(ir, facts, DENY)
    ops = {p.subject: (p.operator.value, p.value)
           for p in goal.constraints}
    assert ops["db.orders.owner"] == ("NE", "session.uid")
    # 对照:特权分支仍是 EQ
    priv = derive_state_goal(ir, facts, PRIV)
    ops_p = {p.subject: (p.operator.value, p.value)
             for p in priv.constraints}
    assert ops_p["db.orders.owner"] == ("EQ", "session.uid")


def test_identity_refresh_step_inserted_with_distinct_value():
    ir, facts = _ir_facts()
    _goal, segment = plan_for_block(ir, facts, DENY)
    actions = [s.action for s in segment.steps]
    assert actions == ["POST op=register", "POST op=create_order",
                       "POST op=register", "POST op=ship_order"]
    users = [s.json.get("user") for s in segment.steps
             if s.action.endswith("register")]
    assert len(set(users)) == 2                    # distinct 身份值
    assert any("identity-refresh" in n for n in segment.notes)


def test_execute_deny_branch_reached():
    ir, facts = _ir_facts()
    _goal, segment = plan_for_block(ir, facts, DENY)
    mod = _load_target_module(SRC)
    res = execute_plan(segment, mod, ir, facts, DENY, SRC,
                       dispatch_arms(SRC))
    assert res.target_covered
    assert res.hits.count("register_ok") == 2      # 双身份构造留痕
    assert "ship_privileged" not in res.hits        # 走的是越权分支
