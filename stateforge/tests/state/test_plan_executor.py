"""Phase 4 验收:定向状态构造(计划→执行→验证→重规划)。

t_state_shop 的 ship_privileged 深块(v0.4 零注入边界靶)经
execute_plan_for_block 全自动判达:参数结构提取(零预设名)、
逐步执行、写块覆盖验证、HITS 双证据;负向口径 = 坏参数被逐步
验证捕获并如实报告;重规划口径 = 残链失败后整链重推导恢复。
"""

from pathlib import Path
from unittest import mock

from stateforge.analysis.static.icfg import build_icfg
from stateforge.core.model import PlanSegment, PlanStep
from stateforge.runtime.plan_executor import (_load_target_module,
                                              execute_plan,
                                              execute_plan_for_block)
from stateforge.state.extractor import extract_state_facts
from stateforge.state.goal_to_plan import dispatch_arms, plan_for_block

SHOP = Path(__file__).parents[2] / "targets" / "stateful"
TARGET = "t_state_shop.py::_handle::91-92::block::1"
SRC = str(SHOP / "t_state_shop.py")


def _ir_facts():
    ir = build_icfg(SHOP)
    facts = extract_state_facts(SHOP, ir=ir)
    return ir, facts


def test_full_chain_reaches_ship_privileged():
    ir, facts = _ir_facts()
    res = execute_plan_for_block(ir, facts, TARGET, SRC)
    assert res.target_covered
    assert "ship_privileged" in res.hits
    assert [s.action for s in res.steps] == [
        "POST op=register", "POST op=create_order", "POST op=ship_order"]
    assert all(s.ok for s in res.steps)


def test_per_step_write_block_verification():
    """前两步各自验证预期写块(状态真实写入),不只看最终命中。"""
    ir, facts = _ir_facts()
    _goal, segment = plan_for_block(ir, facts, TARGET)
    mod = _load_target_module(SRC)
    res = execute_plan(segment, mod, ir, facts, TARGET, SRC,
                       dispatch_arms(SRC))
    reg, cre = res.steps[0], res.steps[1]
    assert reg.ok and reg.write_block          # session.uid/db.users 写块
    assert cre.ok and cre.write_block          # db.orders 写块


def test_params_structurally_extracted_no_preset_names():
    """参数名全部来自源码结构;可选覆盖参数(order 有 or fallback)不填。"""
    ir, facts = _ir_facts()
    _goal, segment = plan_for_block(ir, facts, TARGET)
    by_action = {s.action: s.json for s in segment.steps}
    assert by_action["POST op=register"] == {
        "op": "register", "user": "v", "password": "v"}
    assert by_action["POST op=ship_order"] == {"op": "ship_order"}


def test_bad_param_detected_and_reported():
    """负向:password 空串 → register 写块未覆盖 → 如实报告阻挡。"""
    ir, facts = _ir_facts()
    _goal, good = plan_for_block(ir, facts, TARGET)
    bad = PlanSegment(goal=good.goal, steps=[
        PlanStep(action=s.action,
                 json={**s.json, **({"password": ""}
                                    if s.action.endswith("register")
                                    else {})})
        for s in good.steps])
    mod = _load_target_module(SRC)
    res = execute_plan(bad, mod, ir, facts, TARGET, SRC, dispatch_arms(SRC))
    reg = res.steps[0]
    assert not reg.ok
    assert "write block" in reg.blocking and "guard rejected" in reg.blocking
    assert "ship_privileged" not in res.hits


def test_broken_chain_detected():
    """残链(只有 ship_order)执行失败且被检测(无状态可达深块)。"""
    ir, facts = _ir_facts()
    broken = PlanSegment(goal="x", steps=[PlanStep(
        action="POST op=ship_order", json={"op": "ship_order"})])
    mod = _load_target_module(SRC)
    res = execute_plan(broken, mod, ir, facts, TARGET, SRC,
                       dispatch_arms(SRC))
    assert not res.target_covered
    assert not res.steps[0].ok


def test_replan_recovers_from_broken_chain():
    """首链残(规划缺陷模拟)→ 重规划整链重推导 → 判达恢复。"""
    ir, facts = _ir_facts()
    broken = ("g", PlanSegment(goal="x", steps=[PlanStep(
        action="POST op=ship_order", json={"op": "ship_order"})]))
    full = plan_for_block(ir, facts, TARGET)
    with mock.patch("stateforge.state.goal_to_plan.plan_for_block",
                    side_effect=[broken, full]):
        res = execute_plan_for_block(ir, facts, TARGET, SRC, max_replan=1)
    assert res.replanned
    assert res.target_covered
    assert any(not s.ok for s in res.steps)      # 首链失败步留痕
    assert "ship_privileged" in res.hits


def test_execution_result_shapes():
    ir, facts = _ir_facts()
    res = execute_plan_for_block(ir, facts, TARGET, SRC)
    for s in res.steps:
        assert isinstance(s.request, dict) and "op" in s.request
        assert s.n_covered > 0
