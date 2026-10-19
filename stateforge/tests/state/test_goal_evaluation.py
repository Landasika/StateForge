"""test_goal_evaluation.py —— 联合求值与完整判达分类(PR-03 + PR-03.1)。

PR-03.1 四个 P0 判定漏洞的反例机执行面:
- P0-A:fixed 绑定不被全集顶替(request /orders/12 不得由 order:13 出 witness);
- P0-B:无同点认证探测事实 → 完整判达 UNKNOWN,不出 witness;
  provenance 字符串("symbol-slot:?token")不构成服务器身份确认;
- P0-C:proxy/UNKNOWN/MISSING/exists=False 证据过滤;冲突值与跨点混合拒绝;
- P0-D:path_conditions/unresolvable/trace 进入完整判达分类。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from stateforge.state.goal_derivation import StateGoal
from stateforge.state.goal_evaluation import (
    VERIFIED_ACHIEVED,
    VERIFIED_NOT_ACHIEVED,
    BindingEnv,
    ObservationView,
    classify_achievement,
    evaluate_goal,
)
from stateforge.state.relational import (
    Const,
    Field,
    PathCondition,
    RelOp,
    RelPredicate,
    VarProvenance,
    evaluate_relation,
)
from stateforge.state.runtime_observer import (
    Evidence,
    NOT_OBSERVABLE,
    OBSERVABLE,
    SCOPE_RESOURCE,
    SCOPE_USER,
    StateKey,
    StateObservation,
    UNPROBED,
    observability_of,
)

ORD, USR = Field("?order", "user_id"), Field("?user", "id")
STATUS = Field("?order", "status")
EXISTS_O = Field("?order", "")
REL_EXISTS = RelPredicate(RelOp.EXISTS, EXISTS_O, None, "Is", "app.py", 268)
REL_OWNER = RelPredicate(RelOp.EQ, ORD, USR, "NotEq", "app.py", 271)
REL_PAID = RelPredicate(RelOp.EQ, STATUS, Const("PAID"), "NotEq",
                        "app.py", 273)


def _goal(unresolvable=()):
    return StateGoal(
        target_block="b::ship::275", function="ship_order",
        file="app.py", constraints=(),
        relations=(REL_EXISTS, REL_OWNER, REL_PAID),
        entity_vars=("?",),
        var_provenance=(
            VarProvenance("?order", "orders", "oid"),
            VarProvenance("?user", "users", "username", auth_subject=True),
        ),
        path_conditions=(
            PathCondition("guards", True, "config", "app.py", 270),
            PathCondition("err", False, "local", "app.py", 265),
        ),
        unresolvable=unresolvable)


def _obs(subject, entity, source, value=True, observed_value=None,
         evidence=Evidence.RUNTIME_VERIFIED, value_kind=None,
         observed_at=-1):
    scope = SCOPE_USER if subject.startswith(("User.", "RequestAuth.")) \
        else SCOPE_RESOURCE
    pred = subject if observed_value is None else f"{subject}={observed_value}"
    kind = value_kind or ("NUMBER" if isinstance(observed_value, int)
                          else "ENUM" if observed_value is not None
                          else None)
    return StateObservation(
        key=StateKey(pred, entity, scope), value=value, evidence=evidence,
        source=source, subject=subject,
        observed_value=observed_value, value_kind=kind, observed_at=observed_at)


def _view(rows_orders, rows_users, *, auth=None, complete=("orders", "users"),
          failed=(), point="pt-1", extra=()):
    obs = []
    for oid, uid, st in rows_orders:
        obs.append(_obs("Order.exists", str(oid), "db:orders"))
        obs.append(_obs("Order.status", str(oid), "db:orders",
                        observed_value=st))
        obs.append(_obs("Order.user_id", str(oid), "db:orders",
                        observed_value=uid))
    for name, uid in rows_users:
        obs.append(_obs("User.exists", name, "db:users"))
        obs.append(_obs("User.id", name, "db:users", observed_value=uid))
    if auth is not None:
        obs.append(_obs("RequestAuth.subject", "req", "probe:auth",
                        observed_value=auth))
    return ObservationView.from_observations(
        obs + list(extra), observation_point=point,
        families_complete=complete, families_failed=failed)


FULL_CTX = dict(path_context={"guards": True, "err": False},
                trace_covered=True)


# --------------------------------------------------- 原正例(四分量齐备)

def test_case1_true_owner_and_paid_full_witness():
    r = evaluate_goal(_goal(), _view([(12, 7, "PAID")], [("bob", 7)],
                                     auth="bob"),
                      BindingEnv(fixed={"?order": ["12"], "?user": ["bob"]}),
                      **FULL_CTX)
    assert r.relations_truth == "TRUE"
    assert r.achievement_status == VERIFIED_ACHIEVED
    assert r.witness.binding == (("?order", "orders:12"),
                                 ("?user", "users:bob"))
    assert r.auth_binding_truth == "TRUE"


def test_case2_false_wrong_owner():
    view = _view([(12, 8, "PAID")], [("bob", 7)], auth="bob")
    r = evaluate_goal(_goal(), view,
                      BindingEnv(fixed={"?order": ["12"], "?user": ["bob"]}),
                      **FULL_CTX)
    assert r.relations_truth == "FALSE"
    assert r.achievement_status == VERIFIED_NOT_ACHIEVED
    assert r.failed_constraint and "?order.user_id" in r.failed_constraint


def test_case3_missing_only_exists_false():
    view = _view([], [("bob", 7)], auth="bob")
    r = evaluate_goal(_goal(), view,
                      BindingEnv(fixed={"?order": ["99"], "?user": ["bob"]}),
                      **FULL_CTX)
    assert r.relations_truth == "FALSE"          # 枚举完整 ⇒ EXISTS 反证
    assert r.achievement_status == VERIFIED_NOT_ACHIEVED
    # 谓词级:EXISTS=FALSE(完整枚举),属性=UNKNOWN;无枚举信息 → 全 UNKNOWN
    flat = view.flat_snapshot()
    assert evaluate_relation(REL_EXISTS, flat, {"?order": "orders:99"},
                             view.enumeration_complete) == "FALSE"
    assert evaluate_relation(REL_EXISTS, flat, {"?order": "orders:99"},
                             None) == "UNKNOWN"
    assert evaluate_relation(REL_PAID, flat, {"?order": "orders:99"},
                             view.enumeration_complete) == "UNKNOWN"


def test_case4_unknown_on_observation_failure():
    view = _view([(12, 7, "PAID")], [], auth="bob",
                 complete=("orders",), failed=("users",))
    r = evaluate_goal(_goal(), view,
                      BindingEnv(fixed={"?order": ["12"], "?user": ["bob"]}),
                      **FULL_CTX)
    assert r.relations_truth == "UNKNOWN"
    assert r.achievement_status == "UNKNOWN"
    assert any("family-observation-failed:users" in x
               for x in r.unknown_reasons)


def test_case5_no_witness_from_cross_time_splice():
    t1 = _view([(12, 7, "PENDING")], [("bob", 7)], auth="bob", point="t1")
    t2 = _view([(12, 8, "PAID")], [("bob", 7)], auth="bob", point="t2")
    for view in (t1, t2):
        r = evaluate_goal(_goal(), view,
                          BindingEnv(fixed={"?order": ["12"],
                                            "?user": ["bob"]}),
                          **FULL_CTX)
        assert r.relations_truth != "TRUE"
        assert r.witness is None and r.achievement_status != VERIFIED_ACHIEVED
    with pytest.raises((TypeError, AttributeError)):
        evaluate_goal(_goal(), (t1, t2), {}, **FULL_CTX)  # type: ignore


def test_case6_no_cross_entity_splice():
    view = _view([(7, 7, "PENDING"), (8, 8, "PAID")], [("bob", 7)],
                 auth="bob")
    r = evaluate_goal(_goal(), view,
                      BindingEnv(fixed={"?user": ["bob"]},
                                 enumerable={"?order": ["7", "8"]}),
                      **FULL_CTX)
    assert r.relations_truth == "FALSE"          # 每候选各有一处确定反证
    assert r.witness is None and not r.satisfying
    assert r.achievement_status == VERIFIED_NOT_ACHIEVED


def test_case7_no_implicit_ownership_inference():
    goal = StateGoal(
        target_block="g7", function="f", file="a.py", constraints=(),
        relations=(REL_EXISTS,
                   RelPredicate(RelOp.EQ, Field("?user", "role"),
                                Const("admin"), "Eq", "a.py", 1)),
        var_provenance=(VarProvenance("?order", "orders"),
                        VarProvenance("?user", "users")))
    obs = [_obs("Order.exists", "12", "db:orders"),
           _obs("Order.user_id", "12", "db:orders", observed_value=7),
           _obs("User.exists", "root", "db:users"),
           _obs("User.role", "root", "db:users", observed_value="admin")]
    view = ObservationView.from_observations(
        obs, observation_point="p", families_complete=("orders", "users"))
    r = evaluate_goal(goal, view, trace_covered=True)
    assert r.relations_truth == "TRUE"
    assert r.achievement_status == VERIFIED_ACHIEVED   # 目标无认证分量
    assert not any("user_id" in rel for rel in r.witness.relations)
    r2 = evaluate_goal(_goal(), view, trace_covered=True)
    assert r2.relations_truth != "TRUE"


def test_case8_unprobed_is_unknown_not_permanent():
    obs = [_obs("Order.exists", "12", "db:orders"),
           _obs("Order.status", "12", "db:orders", observed_value="PAID"),
           _obs("Order.user_id", "12", "db:orders", observed_value=7),
           _obs("User.id", "bob", "db:users", observed_value=7)]
    view = ObservationView.from_observations(
        obs, observation_point="p", families_complete=("orders", "users"))
    r = evaluate_goal(_goal(), view, **FULL_CTX)
    assert r.relations_truth == "TRUE"           # 数据在场,关系成立
    # 但无认证探测 → 完整判达 UNKNOWN(不永久 UNVERIFIABLE,补探测即可)
    assert r.achievement_status == "UNKNOWN"
    assert any("auth-association-unverified" in x for x in r.unknown_reasons)


# --------------------------------------------------- P0-A:fixed 不被顶替

def test_p0a_fixed_binding_not_replaced_by_universe():
    """request /orders/12/ship:12 PENDING、13 PAID(owner 同)——
    fixed ?order=12 时不得从 order:13 出 witness。"""
    view = _view([(12, 7, "PENDING"), (13, 7, "PAID")], [("bob", 7)],
                 auth="bob")
    r = evaluate_goal(_goal(), view,
                      BindingEnv(fixed={"?order": ["12"], "?user": ["bob"]}),
                      **FULL_CTX)
    assert r.relations_truth == "FALSE"          # 12 的 status 反证
    assert r.witness is None
    assert r.achievement_status == VERIFIED_NOT_ACHIEVED
    # 对照:存在量词(enumerable)语义下 13 可满足——两种语义显式区分
    r2 = evaluate_goal(_goal(), view,
                       BindingEnv(enumerable={"?order": ["12", "13"]},
                                  fixed={"?user": ["bob"]}),
                       **FULL_CTX)
    assert r2.relations_truth == "TRUE"
    assert dict(r2.witness.binding)["?order"] == "orders:13"


def test_p0a_legacy_binding_candidates_are_fixed_semantics():
    """旧参 binding_candidates = fixed 语义(不与全集并集)。"""
    view = _view([(12, 7, "PENDING"), (13, 7, "PAID")], [("bob", 7)],
                 auth="bob")
    r = evaluate_goal(_goal(), view,
                      {"?order": ["12"], "?user": ["bob"]}, **FULL_CTX)
    assert r.relations_truth == "FALSE" and r.witness is None


# --------------------------------------------------- P0-B:认证分量

def test_p0b_no_auth_probe_no_full_witness():
    """DB 有 bob 且订单属 bob,但本次请求无认证探测 → 完整判达 UNKNOWN。"""
    view = _view([(12, 7, "PAID")], [("bob", 7)], auth=None)
    r = evaluate_goal(_goal(), view,
                      BindingEnv(fixed={"?order": ["12"], "?user": ["bob"]}),
                      **FULL_CTX)
    assert r.relations_truth == "TRUE"
    assert r.achievement_status == "UNKNOWN" and r.witness is None


def test_p0b_probe_mismatch_is_refuted():
    """探测主体=alice、订单属 bob → 认证分量 FALSE → NOT_ACHIEVED。"""
    view = _view([(12, 7, "PAID")], [("bob", 7)], auth="alice")
    r = evaluate_goal(_goal(), view,
                      BindingEnv(fixed={"?order": ["12"], "?user": ["bob"]}),
                      **FULL_CTX)
    assert r.auth_binding_truth == "FALSE"
    assert r.achievement_status == VERIFIED_NOT_ACHIEVED
    assert r.witness is None


def test_p0b_provenance_string_is_not_auth_proof():
    """provenance="symbol-slot:?token" 字符串本身不是服务器身份确认。"""
    view = _view([(12, 7, "PAID")], [("bob", 7)], auth=None)
    r = evaluate_goal(_goal(), view,
                      BindingEnv(fixed={"?order": ["12"], "?user": ["bob"]},
                                 provenance={"?user": "symbol-slot:?token"}),
                      **FULL_CTX)
    assert r.relations_truth == "TRUE"
    assert r.achievement_status == "UNKNOWN" and r.witness is None
    # binding_provenance 仍入结果——审计可见,但不起证明作用
    assert dict(r.binding_provenance)["?user"] == "symbol-slot:?token"


# --------------------------------------------------- P0-C:证据过滤

def test_p0c_proxy_and_unknown_facts_excluded():
    """proxy 证据与 UNKNOWN 值不入视图:不得伪装成确定状态。"""
    extra = [
        _obs("Order.status", "12", "db:orders", observed_value="PAID",
             evidence=Evidence.CONTROL_FLOW_PROXY),        # proxy:排除
        _obs("Order.user_id", "12", "db:orders", observed_value=7,
             value_kind="UNKNOWN"),                          # 未观测:排除
    ]
    view = _view([], [], complete=(), extra=extra)
    assert view.entities == {}                 # 全部被过滤
    r = evaluate_goal(_goal(), view,
                      BindingEnv(fixed={"?order": ["12"]}), **FULL_CTX)
    assert r.relations_truth == "UNKNOWN"


def test_p0c_exists_false_confirms_absence():
    """exists=False → 确认缺席(不建在场实体);EXISTS 可直接反证。"""
    extra = [_obs("Order.exists", "12", "db:orders", value=False)]
    view = _view([], [("bob", 7)], auth="bob", complete=(), extra=extra)
    assert "orders:12" in view.confirmed_absent
    assert "12" not in view.entities.get("orders", {})
    r = evaluate_goal(_goal(), view,
                      BindingEnv(fixed={"?order": ["12"], "?user": ["bob"]}),
                      **FULL_CTX)
    assert r.relations_truth == "FALSE"        # 确认缺席 ⇒ EXISTS FALSE


def test_p0c_conflicting_values_rejected():
    extra = [_obs("Order.status", "12", "db:orders", observed_value="PAID"),
             _obs("Order.status", "12", "db:orders",
                  observed_value="SHIPPED")]
    with pytest.raises(ValueError, match="冲突"):
        _view([], [], complete=(), extra=extra)


def test_p0c_mixed_observation_points_rejected():
    extra = [_obs("Order.status", "12", "db:orders",
                  observed_value="PAID", observed_at=1),
             _obs("Order.user_id", "12", "db:orders",
                  observed_value=7, observed_at=2)]
    with pytest.raises(ValueError, match="观测点"):
        _view([], [], complete=(), extra=extra)


# --------------------------------------------------- P0-D:路径/未解析/Trace

def test_p0d_guards_off_cannot_certify_guarded_chain():
    """path_context guards=False → path 分量 FALSE → 完整判达 UNKNOWN
    (该执行未经 guards=1 守卫链:不能证明,也不能反证)。"""
    view = _view([(12, 7, "PAID")], [("bob", 7)], auth="bob")
    r = evaluate_goal(_goal(), view,
                      BindingEnv(fixed={"?order": ["12"], "?user": ["bob"]}),
                      path_context={"guards": False, "err": False},
                      trace_covered=True)
    assert r.relations_truth == "TRUE"
    assert r.path_conditions_truth == "FALSE"
    assert r.achievement_status == "UNKNOWN" and r.witness is None
    assert any("path-condition-mismatch:guards" in x
               for x in r.unknown_reasons)


def test_p0d_unresolved_goal_cannot_certify():
    """unresolvable 非空 → goal-incomplete:关系可 TRUE,判达不得宣告。"""
    view = _view([(12, 7, "PAID")], [("bob", 7)], auth="bob")
    r = evaluate_goal(_goal(unresolvable=("var:?x", "cause")), view,
                      BindingEnv(fixed={"?order": ["12"], "?user": ["bob"]}),
                      **FULL_CTX)
    assert r.relations_truth == "TRUE"
    assert r.achievement_status == "UNKNOWN" and r.witness is None
    assert any("goal-incomplete" in x for x in r.unknown_reasons)


def test_p0d_trace_false_blocks_witness():
    """状态 TRUE 但 Trace 未见目标块 → trace 分量 FALSE → 不出 witness。"""
    view = _view([(12, 7, "PAID")], [("bob", 7)], auth="bob")
    r = evaluate_goal(_goal(), view,
                      BindingEnv(fixed={"?order": ["12"], "?user": ["bob"]}),
                      path_context={"guards": True, "err": False},
                      trace_covered=False)
    assert r.relations_truth == "TRUE"
    assert r.target_trace_covered == "FALSE"
    assert r.achievement_status == "UNKNOWN" and r.witness is None


def test_p0d_trace_unchecked_is_unknown():
    view = _view([(12, 7, "PAID")], [("bob", 7)], auth="bob")
    r = evaluate_goal(_goal(), view,
                      BindingEnv(fixed={"?order": ["12"], "?user": ["bob"]}),
                      path_context={"guards": True, "err": False})
    assert r.achievement_status == "UNKNOWN"
    assert any("trace-unchecked" in x for x in r.unknown_reasons)


# --------------------------------------------------- 分类器与杂项

def test_classifier_truth_table():
    kw = dict(relations_truth="TRUE", path_conditions_truth="TRUE",
              auth_binding_truth="TRUE", target_trace_covered="TRUE",
              goal_complete=True)
    assert classify_achievement(**kw) == VERIFIED_ACHIEVED
    assert classify_achievement(**{**kw, "relations_truth": "FALSE"}) \
        == VERIFIED_NOT_ACHIEVED
    assert classify_achievement(**{**kw, "auth_binding_truth": "FALSE"}) \
        == VERIFIED_NOT_ACHIEVED
    for comp in ("relations_truth", "path_conditions_truth",
                 "auth_binding_truth", "target_trace_covered"):
        assert classify_achievement(**{**kw, comp: "UNKNOWN"}) == "UNKNOWN"
    assert classify_achievement(**{**kw, "path_conditions_truth": "FALSE"}) \
        == "UNKNOWN"                       # guards=0:不证明也不反证
    assert classify_achievement(**{**kw, "target_trace_covered": "FALSE"}) \
        == "UNKNOWN"                       # 状态-Trace 矛盾交审计
    assert classify_achievement(**{**kw, "goal_complete": False}) == "UNKNOWN"


def test_candidate_cap_yields_unknown_not_false():
    rows = [(i, i, "PENDING") for i in range(100)]
    view = _view(rows, [("bob", 7)], auth="bob")
    r = evaluate_goal(_goal(), view, max_candidates=10, **FULL_CTX)
    assert r.relations_truth == "UNKNOWN"
    assert any("candidate-cap" in x for x in r.unknown_reasons)


def test_witness_serialization():
    from stateforge.state.goal_evaluation import GoalAchievementWitness
    w = GoalAchievementWitness("g", "pt", (("?o", "orders:3"),),
                               ("EXISTS(?order)",),
                               (PathCondition("guards", True, "config"),))
    d = w.to_dict()
    assert d["goal_id"] == "g" and d["binding"] == {"?o": "orders:3"}
    assert d["path_conditions"] == ["path:config:guards=true"]


def test_observability_helpers_attribute_level():
    declared = {"Order.status": OBSERVABLE, "Session.*": NOT_OBSERVABLE}
    assert observability_of(declared, "Order.status") == OBSERVABLE
    assert observability_of(declared, "Order.owner") == UNPROBED
    assert observability_of(declared, "Session.authed_as") == NOT_OBSERVABLE


# --------------------------------------------------- vulnshop 观测器集成

def test_vulnshop_observer_feeds_evaluation(tmp_path):
    from stateforge.targets.vulnshop.state_evidence import \
        VulnshopStateObserver
    db = tmp_path / "v.v.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE users(username TEXT, id INTEGER, role TEXT);"
        "CREATE TABLE orders(id INTEGER, user_id INTEGER, status TEXT);"
        "CREATE TABLE coupons(code TEXT, used INTEGER);"
        "INSERT INTO users VALUES('bob', 7, 'user');"
        "INSERT INTO orders VALUES(12, 7, 'PAID');")
    conn.commit()
    conn.close()

    obs = VulnshopStateObserver(db).observe()
    subjects = {o.subject for o in obs}
    assert {"Order.user_id", "User.id"} <= subjects
    base = dict(observation_point="after-pay",
                families_complete=("orders", "users", "coupons"),
                observability=VulnshopStateObserver.subject_families)

    view = ObservationView.from_observations(obs, **base)
    r = evaluate_goal(_goal(), view,
                      BindingEnv(fixed={"?order": ["12"], "?user": ["bob"]}),
                      **FULL_CTX)
    assert r.relations_truth == "TRUE"
    assert r.achievement_status == "UNKNOWN"    # 观测器无认证探测(P0-B)

    probe = _obs("RequestAuth.subject", "req", "probe:auth",
                 observed_value="bob")
    view2 = ObservationView.from_observations(list(obs) + [probe], **base)
    r2 = evaluate_goal(_goal(), view2,
                       BindingEnv(fixed={"?order": ["12"],
                                         "?user": ["bob"]}), **FULL_CTX)
    assert r2.achievement_status == VERIFIED_ACHIEVED
    assert r2.witness.binding == (("?order", "orders:12"),
                                  ("?user", "users:bob"))
    assert view2.attr_observability("orders", "owner") == UNPROBED
    assert view2.attr_observability("orders", "user_id") == OBSERVABLE
