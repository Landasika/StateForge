"""test_relational.py —— 关系 IR(阶段 1 验收):序列化/比较/结构求值。

对应契约 §6.6.1/§6.6.4 与任务书阶段 1 验收:"可以序列化、反序列化、
比较和求值上述关系;旧 StateGoal 和 StructuredPredicate 的测试保持
通过"(后者由既有测试套件守护)。
"""

from __future__ import annotations

import pytest

from stateforge.state.relational import (
    UNKNOWN,
    Const,
    Field,
    PathCondition,
    RelOp,
    RelPredicate,
    UnresolvableItem,
    VarProvenance,
    evaluate_relation,
    evaluate_relations,
    resolve,
    term_from_dict,
)


def _owner_rel():
    return RelPredicate(
        op=RelOp.EQ,
        left=Field("?order", "user_id", "app.py", 271),
        right=Field("?user", "id", "app.py", 271),
        origin_op="NotEq", source_file="app.py", source_line=271,
        via_edge="false")


def _paid_rel():
    return RelPredicate(
        op=RelOp.EQ,
        left=Field("?order", "status", "app.py", 273),
        right=Const("PAID"),
        origin_op="NotEq", source_file="app.py", source_line=273,
        via_edge="false")


def _exists_rel():
    return RelPredicate(
        op=RelOp.EXISTS, left=Field("?order", "", "app.py", 268),
        right=None, origin_op="Is", source_file="app.py",
        source_line=268, via_edge="false")


SNAPSHOT = {
    "order:12": {"id": 12, "user_id": 7, "status": "PAID"},
    "order:13": {"id": 13, "user_id": 8, "status": "PENDING"},
    "user:7": {"id": 7, "role": "user"},
    "user:8": {"id": 8, "role": "user"},
}
BOUND = {"?order": "order:12", "?user": "user:7"}


# ------------------------------------------------------------- 序列化往返

def test_predicate_roundtrip_field_const():
    rel = _paid_rel()
    again = RelPredicate.from_dict(rel.to_dict())
    assert again == rel
    assert again.render() == rel.render()


def test_predicate_roundtrip_field_field():
    rel = _owner_rel()
    again = RelPredicate.from_dict(rel.to_dict())
    assert again == rel
    assert again.right == Field("?user", "id", "app.py", 271)


def test_predicate_roundtrip_unary_exists():
    rel = _exists_rel()
    again = RelPredicate.from_dict(rel.to_dict())
    assert again == rel
    assert again.right is None


def test_render_is_stable_and_anchored():
    assert _owner_rel().render() == "EQ(?order.user_id, ?user.id)@app.py:271"
    assert _paid_rel().render() == "EQ(?order.status, 'PAID')@app.py:273"
    assert _exists_rel().render() == "EXISTS(?order)@app.py:268"


def test_term_from_dict_distinguishes_kinds():
    f = term_from_dict(Field("?o", "k").to_dict())
    c = term_from_dict(Const("PAID").to_dict())
    assert isinstance(f, Field) and f.name == "k"
    assert isinstance(c, Const) and c.value == "PAID"


def test_aux_ir_roundtrip():
    pc = PathCondition("guards", True, "config", "app.py", 270)
    assert PathCondition.from_dict(pc.to_dict()) == pc
    vp = VarProvenance("?order", "orders", "oid",
                       ("call:get_order_or_none(oid)", "sql:SELECT…"),
                       False, "app.py", 267)
    assert VarProvenance.from_dict(vp.to_dict()) == vp
    u = UnresolvableItem("var:?x", "provenance-unresolved", "app.py", 0)
    assert UnresolvableItem.from_dict(u.to_dict()) == u


# ------------------------------------------------------------- 结构求值

def test_resolve_const_field_unknown():
    assert resolve(Const("PAID"), SNAPSHOT, BOUND) == "PAID"
    assert resolve(Field("?order", "status"), SNAPSHOT, BOUND) == "PAID"
    assert resolve(Field("?order", "total"), SNAPSHOT, BOUND) is UNKNOWN
    assert resolve(Field("?nope", "x"), SNAPSHOT, BOUND) is UNKNOWN
    # 未绑定实体变量 → UNKNOWN(不是 None/False)
    assert resolve(Field("?order", "status"), SNAPSHOT, {}) is UNKNOWN


def test_evaluate_true_case():
    assert evaluate_relation(_owner_rel(), SNAPSHOT, BOUND) == "TRUE"
    assert evaluate_relation(_paid_rel(), SNAPSHOT, BOUND) == "TRUE"
    assert evaluate_relation(_exists_rel(), SNAPSHOT, BOUND) == "TRUE"
    assert evaluate_relations(
        [_owner_rel(), _paid_rel()], SNAPSHOT, BOUND) == "TRUE"


def test_evaluate_false_wrong_owner():
    bad = {"?order": "order:12", "?user": "user:8"}  # 订单属 7,访问者是 8
    assert evaluate_relation(_owner_rel(), SNAPSHOT, bad) == "FALSE"
    assert evaluate_relations(
        [_owner_rel(), _paid_rel()], SNAPSHOT, bad) == "FALSE"


def test_evaluate_false_wrong_status():
    pending = {"?order": "order:13", "?user": "user:8"}
    assert evaluate_relation(_paid_rel(), SNAPSHOT, pending) == "FALSE"


def test_evaluate_unknown_when_ownership_unobserved():
    partial = {"order:12": {"id": 12, "status": "PAID"},  # 缺 user_id 列
               "user:7": {"id": 7}}
    assert evaluate_relation(_owner_rel(), partial, BOUND) == "UNKNOWN"
    # 全 TRUE 不成立但无 FALSE → UNKNOWN,绝不因缺观测翻转成 FALSE/TRUE
    assert evaluate_relations(
        [_owner_rel(), _paid_rel()], partial, BOUND) == "UNKNOWN"


def test_evaluate_exists_semantics_stage1_boundary():
    absent = dict(SNAPSHOT)
    absent.pop("order:12")
    # 阶段 1 边界:实体不在快照 → UNKNOWN(枚举完整性未知),
    # 不按契约 §2.2 判 FALSE——完整 MISSING 语义属阶段 3。
    assert evaluate_relation(_exists_rel(), absent,
                             {"?order": "order:12"}) == "UNKNOWN"
    # NOT_EXISTS:在场可证伪;不在场证不了"不存在" → UNKNOWN
    ne = RelPredicate(op=RelOp.NOT_EXISTS, left=Field("?order", ""))
    assert evaluate_relation(ne, SNAPSHOT, BOUND) == "FALSE"
    assert evaluate_relation(ne, absent,
                             {"?order": "order:12"}) == "UNKNOWN"


def test_evaluate_truthy_and_null():
    row = {"order:14": {"id": 14, "note": None}}
    b = {"?order": "order:14"}
    truthy = RelPredicate(op=RelOp.TRUTHY, left=Field("?order", "note"))
    assert evaluate_relation(truthy, row, b) == "FALSE"   # NULL → falsy
    missing = RelPredicate(op=RelOp.TRUTHY, left=Field("?order", "gone"))
    assert evaluate_relation(missing, row, b) == "UNKNOWN"


def test_unknown_sentinel_refuses_bool():
    with pytest.raises(TypeError):
        if UNKNOWN:                                     # noqa: B018
            pass


def test_state_goal_carries_relational_fields():
    """StateGoal(§6.6 扩展)序列化含新键且可读回。"""
    from stateforge.state.goal_derivation import StateGoal

    goal = StateGoal(
        target_block="b", function="f", file="app.py", constraints=(),
        relations=(_owner_rel(), _paid_rel()),
        entity_vars=("?order", "?user"),
        var_provenance=(VarProvenance("?order", "orders", "oid"),),
        path_conditions=(PathCondition("guards", True, "config"),),
        unresolvable=(UnresolvableItem("var:?x", "cause"),),
    )
    d = goal.to_dict()
    for key in ("relations", "entity_vars", "var_provenance",
                "path_conditions", "unresolvable"):
        assert key in d
    assert RelPredicate.from_dict(d["relations"][0]) == _owner_rel()
    # 旧字段语义不变:constraints 恒在
    assert d["constraints"] == []


# ------------------------------------------- PR-03:MISSING 语义(枚举完整)

def test_exists_false_only_with_complete_enumeration():
    """实体缺席 + 枚举完整 → EXISTS=FALSE;无枚举信息 → UNKNOWN。"""
    ex = RelPredicate(op=RelOp.EXISTS, left=Field("?order", ""))
    snap = {"orders:3": {"id": 3}}
    b = {"?order": "orders:99"}
    assert evaluate_relation(ex, snap, b,
                             {"orders": True}) == "FALSE"
    assert evaluate_relation(ex, snap, b, None) == "UNKNOWN"
    assert evaluate_relation(ex, snap, b, {"orders": False}) == "UNKNOWN"
    # NOT_EXISTS 对偶:完整枚举下缺席 → TRUE
    ne = RelPredicate(op=RelOp.NOT_EXISTS, left=Field("?order", ""))
    assert evaluate_relation(ne, snap, b, {"orders": True}) == "TRUE"
    assert evaluate_relation(ne, snap, {"?order": "orders:3"},
                             {"orders": True}) == "FALSE"


def test_null_is_observed_not_missing():
    """NULL 是已观测值:TRUTHY→FALSE,EQ(None) 可判;≠ UNKNOWN/缺席。"""
    snap = {"orders:5": {"note": None}}
    b = {"?order": "orders:5"}
    truthy = RelPredicate(op=RelOp.TRUTHY, left=Field("?order", "note"))
    assert evaluate_relation(truthy, snap, b) == "FALSE"
    eq_none = RelPredicate(op=RelOp.EQ, left=Field("?order", "note"),
                           right=Const(None))
    assert evaluate_relation(eq_none, snap, b) == "TRUE"
    gone = RelPredicate(op=RelOp.EQ, left=Field("?order", "gone"),
                        right=Const("x"))
    assert evaluate_relation(gone, snap, b) == "UNKNOWN"  # 字段缺=未观测
