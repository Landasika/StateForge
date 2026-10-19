"""test_goal_evaluation.py —— PR-03:联合求值(唯一目标判定入口)。

任务书八项强制反例 + witness/UNPROBED/枚举上限/认证关联的机器执行面。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from stateforge.state.goal_derivation import StateGoal
from stateforge.state.goal_evaluation import (
    GoalAchievementWitness,
    ObservationView,
    evaluate_goal,
)
from stateforge.state.relational import (
    Const,
    Field,
    RelOp,
    RelPredicate,
    VarProvenance,
)
from stateforge.state.runtime_observer import (
    NOT_OBSERVABLE,
    OBSERVABLE,
    UNPROBED,
    observability_of,
)

ORD, USR = Field("?order", "user_id"), Field("?user", "id")
STATUS = Field("?order", "status")
EXISTS_O = Field("?order", "")


def _goal():
    return StateGoal(
        target_block="b::ship::275", function="ship_order",
        file="app.py", constraints=(),
        relations=(
            RelPredicate(RelOp.EXISTS, EXISTS_O, None, "Is", "app.py", 268),
            RelPredicate(RelOp.EQ, ORD, USR, "NotEq", "app.py", 271),
            RelPredicate(RelOp.EQ, STATUS, Const("PAID"), "NotEq",
                         "app.py", 273),
        ),
        entity_vars=("?",),
        var_provenance=(
            VarProvenance("?order", "orders", "oid"),
            VarProvenance("?user", "users", "username", auth_subject=True),
        ))


def _view(rows_orders, rows_users, *, complete=("orders", "users"),
          failed=(), point="pt-1"):
    obs = []
    for oid, uid, st in rows_orders:
        obs.append(_obs("Order.exists", str(oid), "db:orders", True, None))
        obs.append(_obs("Order.status", str(oid), "db:orders", True, st))
        obs.append(_obs("Order.user_id", str(oid), "db:orders", True, uid))
    for name, uid in rows_users:
        obs.append(_obs("User.exists", name, "db:users", True, None))
        obs.append(_obs("User.id", name, "db:users", True, uid))
    return ObservationView.from_observations(
        obs, observation_point=point,
        families_complete=complete, families_failed=failed)


def _obs(subject, entity, source, value, observed_value):
    from stateforge.state.runtime_observer import (
        Evidence, SCOPE_RESOURCE, SCOPE_USER, StateKey, StateObservation)
    scope = SCOPE_USER if subject.startswith("User.") else SCOPE_RESOURCE
    kind = "NUMBER" if isinstance(observed_value, int) else "ENUM"
    return StateObservation(
        key=StateKey(f"{subject}"
                     + (f"={observed_value}" if observed_value is not None
                        else ""), entity, scope),
        value=value, evidence=Evidence.RUNTIME_VERIFIED, source=source,
        subject=subject, observed_value=observed_value, value_kind=kind)


GOOD_VIEW = _view([(12, 7, "PAID")], [("bob", 7)])


# ------------------------------------------------------------ 强制反例

def test_case1_true_owner_and_paid():
    """同一订单 owner=当前用户且 PAID → TRUE + witness。"""
    r = evaluate_goal(_goal(), GOOD_VIEW,
                      {"?order": ["12"], "?user": ["bob"]})
    assert r.status == "TRUE"
    assert r.witness.binding == (("?order", "orders:12"),
                                 ("?user", "users:bob"))
    assert r.witness.observation_point == "pt-1"
    assert len(r.witness.relations) == 3


def test_case2_false_wrong_owner():
    """订单属另一用户 → FALSE(关系两侧已可靠观测)。"""
    view = _view([(12, 8, "PAID")], [("bob", 7)])   # order12 属 uid=8
    r = evaluate_goal(_goal(), view, {"?order": ["12"], "?user": ["bob"]})
    assert r.status == "FALSE"
    assert r.failed_constraint and "?order.user_id" in r.failed_constraint


def test_case3_missing_only_exists_false():
    """订单不存在且枚举完整 → EXISTS=FALSE;属性谓词=UNKNOWN。"""
    view = _view([], [("bob", 7)])                  # orders 空,枚举完整
    r = evaluate_goal(_goal(), view, {"?order": ["99"], "?user": ["bob"]})
    # 目标级:枚举完整 ⇒ "存在满足订单"被可靠反证(EXISTS=FALSE)→ FALSE;
    # 谓词级:EXISTS=FALSE、属性谓词=UNKNOWN(下方直证)——两者并行不悖
    assert r.status == "FALSE"
    assert r.failed_constraint and "EXISTS" in r.failed_constraint
    # 直接验证谓词级:EXISTS 反证,FALSE;枚举不完整时二者皆 UNKNOWN
    from stateforge.state.relational import evaluate_relation
    flat = view.flat_snapshot()
    ex = RelPredicate(RelOp.EXISTS, EXISTS_O, None)
    assert evaluate_relation(ex, flat, {"?order": "orders:99"},
                             view.enumeration_complete) == "FALSE"
    assert evaluate_relation(ex, flat, {"?order": "orders:99"},
                             None) == "UNKNOWN"     # 无枚举完整性 → 不猜
    st = RelPredicate(RelOp.EQ, STATUS, Const("PAID"))
    assert evaluate_relation(st, flat, {"?order": "orders:99"},
                             view.enumeration_complete) == "UNKNOWN"


def test_case4_unknown_on_observation_failure():
    """状态/身份观测失败 → UNKNOWN(failed_families ≠ 枚举完整)。"""
    view = _view([(12, 7, "PAID")], [],             # users 读失败
                 complete=("orders",), failed=("users",))
    r = evaluate_goal(_goal(), view, {"?order": ["12"], "?user": ["bob"]})
    assert r.status == "UNKNOWN"
    assert any("family-observation-failed:users" in x
               for x in r.unknown_reasons)


def test_case5_no_witness_from_cross_time_splice():
    """t1 owner 正确(t 但 PENDING)、t2 PAID(owner 错)→ 皆非 TRUE,
    无任何 API 路径把两点拼成 witness(单视图结构约束)。"""
    t1 = _view([(12, 7, "PENDING")], [("bob", 7)], point="t1")
    t2 = _view([(12, 8, "PAID")], [("bob", 7)], point="t2")
    for view in (t1, t2):
        r = evaluate_goal(_goal(), view,
                          {"?order": ["12"], "?user": ["bob"]})
        assert r.status != "TRUE" and r.witness is None
    # 两个视图各自判定——evaluate_goal 单视图入口,多视图输入即类型错误
    # (拼接被结构性排除,不是靠约定)
    with pytest.raises((TypeError, AttributeError)):
        evaluate_goal(_goal(), (t1, t2), {})         # type: ignore[arg-type]


def test_case6_no_cross_entity_splice():
    """order:7 满足 owner、order:8 满足 PAID → 不可拼接成功。"""
    view = _view([(7, 7, "PENDING"), (8, 8, "PAID")], [("bob", 7)])
    r = evaluate_goal(_goal(), view, {"?user": ["bob"]})   # order 全枚举
    # 每候选各有一处**确定**反证(7:status≠PAID;8:owner≠bob)→ 目标 FALSE;
    # 关键:绝不因 7 的 owner 真而拼上 8 的 PAID 判 TRUE(不可拼接成功)
    assert r.status == "FALSE"
    assert r.witness is None and not r.satisfying


def test_case7_no_implicit_ownership_inference():
    """独立存在订单与管理员,目标未声明归属 → 只判目标所述,不推导归属。"""
    goal = StateGoal(                                   # 无 EQ(user_id,id)
        target_block="g7", function="f", file="a.py", constraints=(),
        relations=(
            RelPredicate(RelOp.EXISTS, EXISTS_O, None),
            RelPredicate(RelOp.EQ, Field("?user", "role"),
                         Const("admin"), "Eq", "a.py", 1),
        ),
        var_provenance=(VarProvenance("?order", "orders"),
                        VarProvenance("?user", "users")))
    obs = []
    obs.append(_obs("Order.exists", "12", "db:orders", True, None))
    obs.append(_obs("Order.user_id", "12", "db:orders", True, 7))
    obs.append(_obs("User.exists", "root", "db:users", True, None))
    obs.append(_obs("User.role", "root", "db:users", True, "admin"))
    view = ObservationView.from_observations(
        obs, observation_point="p", families_complete=("orders", "users"))
    r = evaluate_goal(goal, view)
    assert r.status == "TRUE"          # 目标所述成立…
    # …但结果不含任何归属断言:12 号单与 root 的关联未被"推导"出来
    assert not any("user_id" in rel for rel in r.witness.relations)
    # 目标显式声明归属时才验证归属(同一视图下判 FALSE/UNKNOWN)
    r2 = evaluate_goal(_goal(), view)
    assert r2.status != "TRUE"


def test_case8_unprobed_is_unknown_not_permanent_unverifiable():
    """UNPROBED(未声明观测能力)→ UNKNOWN;换声明后同一视图可判 TRUE
    (不永久 UNVERIFIABLE)。"""
    obs = [_obs("Order.exists", "12", "db:orders", True, None),
           _obs("Order.status", "12", "db:orders", True, "PAID"),
           _obs("Order.user_id", "12", "db:orders", True, 7),
           _obs("User.id", "bob", "db:users", True, 7)]
    goal = StateGoal(
        target_block="g8", function="f", file="a.py", constraints=(),
        relations=(RelPredicate(RelOp.EXISTS, EXISTS_O, None),
                   RelPredicate(RelOp.EQ, ORD, USR, "NotEq", "a.py", 2),
                   RelPredicate(RelOp.EQ, STATUS, Const("PAID"), "NotEq",
                                "a.py", 3)),
        var_provenance=(VarProvenance("?order", "orders"),
                        VarProvenance("?user", "users")))
    unprobed = ObservationView.from_observations(
        obs, observation_point="p", families_complete=("orders", "users"))
    r = evaluate_goal(goal, unprobed)
    assert r.status == "TRUE"          # 数据在场即可判——UNPROBED 只标注
    # 字段未观测 + 未声明 → UNKNOWN + attr-unprobed 原因
    partial = ObservationView.from_observations(
        obs[:2], observation_point="p",
        families_complete=("orders",))          # 缺 user_id 观测
    r2 = evaluate_goal(goal, partial)
    assert r2.status == "UNKNOWN"
    assert any("no-candidates" in x or "attr-unprobed" in x
               for x in r2.unknown_reasons)


# --------------------------------------------------- 补充:witness/上限/认证

def test_witness_serialization():
    w = GoalAchievementWitness("g", "pt", (("?o", "orders:3"),),
                               ("EXISTS(?order)",))
    d = w.to_dict()
    assert d["goal_id"] == "g" and d["binding"] == {"?o": "orders:3"}
    assert d["relations"] == ["EXISTS(?order)"]


def test_candidate_cap_yields_unknown_not_false():
    """候选空间超上限 → 前 N 穷尽无满足 → UNKNOWN(不轻易 FALSE)。"""
    rows = [(i, i, "PENDING") for i in range(100)]   # 100 订单全 PENDING
    view = _view(rows, [("bob", 7)])
    r = evaluate_goal(_goal(), view, max_candidates=10)
    assert r.status == "UNKNOWN"
    assert any("candidate-cap" in x for x in r.unknown_reasons)


def test_auth_subject_requires_auth_binding_provenance():
    """DB 存在 user 行 ≠ 请求认证为它:绑定来源无认证证据 → 标注未验证。"""
    r = evaluate_goal(_goal(), GOOD_VIEW,
                      {"?order": ["12"], "?user": ["bob"]})
    assert r.status == "TRUE"
    assert r.auth_subject_verified is False         # 无 provenance 输入
    assert any("auth-association-unverified" in x
               for x in r.unknown_reasons)
    r2 = evaluate_goal(_goal(), GOOD_VIEW,
                       {"?order": ["12"], "?user": ["bob"]},
                       binding_provenance={"?user": "symbol-slot:?token"})
    assert r2.auth_subject_verified is True         # 认证来源(槽回填)


def test_observability_helpers_attribute_level():
    declared = {"Order.status": OBSERVABLE, "Session.*": NOT_OBSERVABLE}
    assert observability_of(declared, "Order.status") == OBSERVABLE
    assert observability_of(declared, "Order.owner") == UNPROBED  # 不外推
    assert observability_of(declared, "Session.authed_as") == NOT_OBSERVABLE
    assert observability_of({}, "Any.thing") == UNPROBED


# --------------------------------------------------- vulnshop 观测器集成

def test_vulnshop_observer_feeds_evaluation(tmp_path):
    """真观测器 → 视图 → ship 目标判 TRUE(归属/状态数据面打通)。"""
    from stateforge.targets.vulnshop.state_evidence import \
        VulnshopStateObserver
    db = tmp_path / "v.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE users(username TEXT, id INTEGER, role TEXT);"
        "CREATE TABLE orders(id INTEGER, user_id INTEGER, status TEXT);"
        "CREATE TABLE coupons(code TEXT, used INTEGER);"
        "INSERT INTO users VALUES('bob', 7, 'user');"
        "INSERT INTO orders VALUES(12, 7, 'PAID');")
    conn.commit()
    conn.close()

    obs = VulnshopStateObserver(db)
    observations = obs.observe()
    subjects = {o.subject for o in observations}
    assert {"Order.user_id", "User.id"} <= subjects        # PR-03 新字段
    view = ObservationView.from_observations(
        observations, observation_point="after-pay",
        families_complete=("orders", "users", "coupons"),
        observability=VulnshopStateObserver.subject_families)
    r = evaluate_goal(_goal(), view, {"?order": ["12"], "?user": ["bob"]})
    assert r.status == "TRUE"
    assert r.witness.binding == (("?order", "orders:12"),
                                 ("?user", "users:bob"))
    # 属性级声明:Order.owner 未声明 → UNPROBED(不外推)
    assert view.attr_observability("orders", "owner") == UNPROBED
    assert view.attr_observability("orders", "user_id") == OBSERVABLE
