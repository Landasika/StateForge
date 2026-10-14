"""Phase 2(MVP)验收:目标基本块 → StateGoal(t_state_shop 旗舰靶)。

顾问 Phase-2 口径:不手工指定业务状态,仅给定目标代码位置,生成候选
状态条件。ship_privileged 深块的必要约束应覆盖:
- EXISTS db.orders(订单存在,L87 None 门)
- EXISTS session.uid(会话已注册,L90)
- EQ db.orders.owner(归属绑定,L90,值侧动态→None 保留主体)
"""

from pathlib import Path

from stateforge.analysis.static.icfg import build_icfg
from stateforge.state.extractor import extract_state_facts
from stateforge.state.goal_derivation import (StateGoal, derive_state_goal,
                                              necessary_guards)

SHOP = Path(__file__).parents[2] / "targets" / "stateful"
TARGET = "t_state_shop.py::_handle::91-92::block::1"   # ship_privileged 块


def _goal() -> StateGoal:
    ir = build_icfg(SHOP)
    facts = extract_state_facts(SHOP)
    return derive_state_goal(ir, facts, TARGET)


def test_necessary_guards_include_ownership_branch():
    ir = build_icfg(SHOP)
    guards = necessary_guards(ir, TARGET)
    assert any("90-90" in g for g in guards)       # 归属 elif
    assert any("87-87" in g for g in guards)       # None 门


def test_goal_constraints_cover_state_domains():
    goal = _goal()
    subjects = {p.subject for p in goal.constraints}
    assert "db.orders" in subjects                 # EXISTS 订单
    assert "session.uid" in subjects               # EXISTS 会话
    assert "db.orders.owner" in subjects           # EQ 归属


def test_goal_ownership_eq_and_existence_ops():
    goal = _goal()
    ops = {(p.subject, p.operator.value) for p in goal.constraints}
    assert ("db.orders", "EXISTS") in ops
    assert ("db.orders.owner", "EQ") in ops


def test_goal_evidence_traces_to_source_lines():
    goal = _goal()
    joined = "\n".join(goal.evidence)
    assert ":87 db.orders EXISTS" in joined
    assert ":90 db.orders.owner EQ" in joined


def test_goal_serializable():
    goal = _goal()
    d = goal.to_dict()
    assert d["target_block"] == TARGET
    assert d["function"] == "_handle"
    assert len(d["constraints"]) >= 3


def test_unknown_block_returns_none():
    ir = build_icfg(SHOP)
    facts = extract_state_facts(SHOP)
    assert derive_state_goal(ir, facts, "no-such-block") is None
