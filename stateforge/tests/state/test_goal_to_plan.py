"""Phase 2/3 桥接验收:目标块 → 自动三步链(t_state_shop 旗舰靶)。

顾问 Phase-3 口径:能自动生成 create→pay→ship 等多步序列,而不是
依赖预设模板。t_state_shop 的 ship_privileged 深块(单请求词法不可达)
经 goal_to_plan 一条龙产出 [register, create_order, ship_order]——
全程无 step_template/无 _JSON_FILL/无认证链补全(合成模型无这些结构)。
"""

from pathlib import Path

from stateforge.analysis.static.icfg import build_icfg
from stateforge.state.extractor import extract_state_facts
from stateforge.state.goal_to_plan import (dispatch_arms, plan_for_block,
                                           synthesize_appmodel)
from stateforge.state.goal_derivation import derive_state_goal

SHOP = Path(__file__).parents[2] / "targets" / "stateful"
TARGET = "t_state_shop.py::_handle::91-92::block::1"


def _plan():
    ir = build_icfg(SHOP)
    facts = extract_state_facts(SHOP)
    return plan_for_block(ir, facts, TARGET)


def test_dispatch_arms_discovered():
    arms = dict((n, (s, e)) for n, s, e in
                dispatch_arms(str(SHOP / "t_state_shop.py")))
    for expected in ("register", "create_order", "ship_order"):
        assert expected in arms, f"分派臂 {expected} 未发现"


def test_plan_is_three_step_chain():
    result = _plan()
    assert result is not None
    _goal, segment = result
    actions = [s.action for s in segment.steps]
    assert actions == ["POST op=register", "POST op=create_order",
                       "POST op=ship_order"]


def test_plan_dependency_order():
    """register(产 uid)先于 create_order(需 uid 产 order)先于
    ship_order(需 order)——链序与状态依赖一致。"""
    _goal, segment = _plan()
    actions = [s.action for s in segment.steps]
    assert actions.index("POST op=register") < actions.index(
        "POST op=create_order") < actions.index("POST op=ship_order")


def test_final_action_annotated():
    _goal, segment = _plan()
    assert any("final-action: ship_order" in n for n in segment.notes)


def test_synthesized_model_shapes():
    ir = build_icfg(SHOP)
    facts = extract_state_facts(SHOP)
    goal = derive_state_goal(ir, facts, TARGET)
    model = synthesize_appmodel(ir, facts, goal)
    routes = {ep.route for ep in model.endpoints}
    assert {"op=register", "op=create_order", "op=ship_order"} <= routes
    register = next(ep for ep in model.endpoints
                    if ep.route == "op=register")
    eff_vars = {e.var for e in register.effects}
    assert "session.uid" in eff_vars          # 会话生产者
    ship = next(ep for ep in model.endpoints if ep.route == "op=ship_order")
    pre_vars = {p.var for p in ship.preconditions}
    assert "db.orders.owner" in pre_vars      # 归属门


def test_deep_block_unreachable_single_request_is_targeted():
    """目标块选择的正是 91-92(ship_privileged)——单请求词法不可达深块。"""
    ir = build_icfg(SHOP)
    blk = ir.blocks[TARGET]
    assert "ship_privileged" in "".join(blk.statements)
