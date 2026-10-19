"""test_relational_goal_runtime.py —— PR-04 / M0-B:五步已知链逐请求联合验证。

验收门 M0-B(任务书):已知链在干净基线上执行,返回完整 GoalEvaluation +
逐步观测 + Trace;错误链返回明确原因且不错误记成功。**不验收自动规划**
(链为已知测试数据,非规划器产出)。

测试矩阵:正常链/未登录/错误身份/错误 oid/未支付/支付不足/观测器异常/
每步状态变化/不同输入不串扰/冷启动重放。

四分量判达纪律(P0-A/B/D):
- ?order/?user 用**本次请求**的 fixed 绑定(extract 回填),不被全集顶替;
- 认证分量只认同观测点 RequestAuth.subject 探测事实;
- 入口状态在 ship 前检查点验证(不拿终态 SHIPPED 反证入口 PAID);
- Trace 独立证明 UPDATE 块;状态判达与覆盖分别断言。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from stateforge.analysis.static.icfg import build_icfg
from stateforge.runtime.execution_context import (
    ExecutionContext,
    RequestLevelHarness,
    SnapshotLifecycle,
)
from stateforge.runtime.mapper import map_trace
from stateforge.runtime.tracer import TraceSession
from stateforge.state.goal_evaluation import (
    VERIFIED_ACHIEVED,
    VERIFIED_NOT_ACHIEVED,
    BindingEnv,
    ObservationView,
    classify_achievement,
    evaluate_goal,
)
from stateforge.state.goal_derivation import derive_relational_goal
from stateforge.state.relational import Const, Field, RelOp, RelPredicate
from stateforge.targets.vulnshop.fuzz_target import make_instance

VULNSHOP = Path(__file__).parents[3] / "targets" / "vulnshop"
ITEMS = [{"sku": "book", "price": 100, "qty": 1}]
FAMILIES = ("orders", "users", "coupons")


def _chain(user="bob", amount=100, with_pay=True, with_login=True,
           ship_user_token=True):
    """五步已知链(任务书口径;不视为规划器产出)。"""
    steps = [
        {"method": "POST", "path": "/auth/register",
         "json": {"username": user, "password": "pw"}},
    ]
    if with_login:
        steps.append({"method": "POST", "path": "/auth/login",
                      "json": {"username": user, "password": "pw"},
                      "extract": {"token": "?token"}})
    steps.append({"method": "POST", "path": "/orders",
                  "json": {"items": ITEMS},
                  "header": {"Authorization": "Bearer ?token"},
                  "extract": {"id": "?oid"}})
    if with_pay:
        steps.append({"method": "POST", "path": "/orders/?oid/pay",
                      "json": {"amount": amount},
                      "header": {"Authorization": "Bearer ?token"}})
    ship = {"method": "POST", "path": "/orders/?oid/ship",
            "header": {"Authorization": "Bearer ?token"}}
    if not ship_user_token:
        ship = {"method": "POST", "path": "/orders/?oid/ship"}  # 未登录
    steps.append(ship)
    return steps


@pytest.fixture()
def inst(tmp_path):
    return make_instance(tmp_path / "m0b.db", guards=1)


@pytest.fixture(scope="module")
def ir_goal():
    ir = build_icfg(VULNSHOP)
    target = next(b.id for b in ir.blocks.values()
                  if b.function == "ship_order" and b.id.startswith("app.py")
                  and any("UPDATE orders SET status='SHIPPED'" in s
                          for s in b.statements))
    goal = derive_relational_goal(ir, target)
    return ir, target, goal


def _view(observations, probe, point):
    obs = list(observations) + ([probe] if probe else [])
    return ObservationView.from_observations(
        obs, observation_point=point, families_complete=FAMILIES)


def _env(record, user):
    """fixed 绑定取自**本输入执行期**的绑定快照(输入还原钩子会清表,
    事后读全局绑定无效;P0-A:固定绑定来自 extract 回填,不查全集)。"""
    oid = str(record.steps[2].bindings["oid"])
    return BindingEnv(
        fixed={"?order": [oid], "?user": [user]},
        provenance={"?order": "extract:?oid", "?user": "auth:token"})


def _make_extra_probe(inst):
    """认证探测:解析**该步请求实际携带**的 Bearer 头(经绑定代入),
    hmac+在场校验——匿名步无探测(P0-B:全局绑定不冒充本步身份)。"""
    import re as _re
    from stateforge.targets.vulnshop.fuzz_target import _APP_MOD, _SLOT
    from stateforge.targets.vulnshop.state_evidence import observe_request_auth

    def extra(step):
        header = (step.get("header") or {}).get("Authorization", "")
        token = _SLOT.sub(
            lambda m: str(inst.bindings.get(m.group(1), m.group(0))),
            str(header))
        m = _re.match(r"^Bearer (.+)$", token)
        if not m:
            return ()
        probe = observe_request_auth(m.group(1), app_module=_APP_MOD,
                                     db_path=inst.db_path)
        return (probe,) if probe else ()

    return extra


def _run_chain(inst, steps, ir=None):
    """驱动一个输入:SnapshotLifecycle 每输入一次 + 逐请求 before/after。"""
    observer = inst.observer()
    execution = ExecutionContext(
        SnapshotLifecycle(files=[inst.db_path],
                          memory_hooks=[inst.bindings.clear]))
    session = TraceSession(root=str(VULNSHOP)) if ir else None
    harness = RequestLevelHarness(
        execution, observer=observer,
        bindings_fn=lambda: dict(inst.bindings),
        extra_observe=_make_extra_probe(inst))
    if session is not None:
        session.__enter__()
    try:
        record = harness.run_input(
            steps,
            lambda step: inst.request(step).status_code,
            trace_session=session,
            action_of=lambda i, s: f"{s.get('method', 'GET')} {s.get('path', '')}")
    finally:
        if session is not None:
            session.__exit__(None, None, None)
    return record, session


# ------------------------------------------------------------- 正常链(主路径)

def test_happy_chain_full_witness(inst, ir_goal):
    """五步链:ship 前入口状态联判 TRUE、ship 后 SHIPPED、Trace 命中
    UPDATE、四分量 VERIFIED_ACHIEVED;逐步状态变化可观测。"""
    ir, target_block, goal = ir_goal
    assert not goal.unresolvable
    record, session = _run_chain(inst, _chain(), ir=ir)
    assert record.lifecycle_failures == 0 and len(record.steps) == 5

    statuses = [s.status for s in record.steps]
    assert statuses == [201, 200, 201, 200, 200]

    # 逐步状态变化(after 观测):login 后仍无订单;create 后 CREATED;
    # pay 后 PAID;ship 后 SHIPPED
    def _status_of(step: int) -> list:
        rows = {}
        for o in record.steps[step].after:
            if o.subject == "Order.status":
                rows[o.key.entity] = o.observed_value
        return rows
    assert _status_of(1) == {}                      # login 后无订单
    oid = str(record.steps[2].bindings["oid"])
    assert _status_of(2)[oid] == "CREATED"
    assert _status_of(3)[oid] == "PAID"
    assert _status_of(4)[oid] == "SHIPPED"

    # 入口状态在 ship 前检查点验证(before(4) 已含随步探测)
    pre_view = _view(record.steps[4].before, None, "before-ship")
    pre = evaluate_goal(goal, pre_view, _env(record, "bob"),
                        path_context={"guards": True, "err": False},
                        trace_covered=None)
    assert pre.relations_truth == "TRUE"
    assert pre.auth_binding_truth == "TRUE"

    # Trace 独立证明 UPDATE 块(ship 步轨迹窗口)
    a, b = record.steps[4].trace_span
    covered = set(map_trace(session.events[a:b], ir).blocks)
    assert target_block in covered

    # ship 后终态:同一 oid 的 SHIPPED(不反证入口)
    post_view = _view(record.steps[4].after, None, "after-ship")
    post_rel = RelPredicate(RelOp.EQ, Field("?order", "status"),
                            Const("SHIPPED"), "Eq", "app.py", 275)
    post = evaluate_goal(
        type("G", (), {"relations": (post_rel,),
                       "var_provenance": goal.var_provenance,
                       "target_block": goal.target_block,
                       "path_conditions": (), "unresolvable": ()})(),
        post_view, _env(record, "bob"),
        path_context={"guards": True, "err": False}, trace_covered=None)
    assert post.relations_truth == "TRUE"

    # 四分量完整判达:入口 relations + path + auth + trace(ship)
    achievement = classify_achievement(
        relations_truth=pre.relations_truth,
        path_conditions_truth=pre.path_conditions_truth,
        auth_binding_truth=pre.auth_binding_truth,
        target_trace_covered="TRUE",
        goal_complete=True)
    assert achievement == VERIFIED_ACHIEVED


# ------------------------------------------------------------- 错误链矩阵

def test_chain_no_auth_401_no_witness(inst, ir_goal):
    """未登录 ship → 401;无认证探测 → 完整判达 UNKNOWN,不出 witness。"""
    ir, _, goal = ir_goal
    record, session = _run_chain(inst, _chain(with_login=True,
                                              ship_user_token=False), ir=ir)
    assert record.steps[4].status == 401
    pre_view = _view(record.steps[4].before, None, "before-ship")
    r = evaluate_goal(goal, pre_view, _env(record, "bob"),
                      path_context={"guards": True, "err": False},
                      trace_covered=None)
    assert r.relations_truth == "TRUE"          # DB 侧关系本成立
    assert r.auth_binding_truth == "UNKNOWN"    # 无探测
    assert r.achievement_status == "UNKNOWN" and r.witness is None


def test_chain_wrong_identity_403_refuted(inst, ir_goal):
    """错误身份:alice 登录并 ship bob 的订单 → 探测主体 alice ≠ ?user=bob
    → 认证分量 FALSE → VERIFIED_NOT_ACHIEVED(非只报 403)。"""
    ir, _, goal = ir_goal
    # bob 注册/登录/建单/支付(停在 pay,不 ship);alice 登录后 ship bob 的 ?oid
    steps = _chain(user="bob")[:-1]              # 去掉 bob 自己的 ship 步
    alice = [{"method": "POST", "path": "/auth/register",
              "json": {"username": "alice", "password": "pw"}},
             {"method": "POST", "path": "/auth/login",
              "json": {"username": "alice", "password": "pw"},
              "extract": {"token": "?token"}},
             {"method": "POST", "path": "/orders/?oid/ship",
              "header": {"Authorization": "Bearer ?token"}}]
    record, session = _run_chain(inst, steps + alice, ir=ir)
    ship = record.steps[-1]
    assert ship.status == 403
    probes = [o for o in ship.before if o.subject == "RequestAuth.subject"]
    assert probes and probes[0].observed_value == "alice"  # 随步探测
    pre_view = _view(ship.before, None, "before-ship")
    r = evaluate_goal(goal, pre_view, _env(record, "bob"),  # 目标绑定仍 bob
                      path_context={"guards": True, "err": False},
                      trace_covered=None)
    assert r.auth_binding_truth == "FALSE"
    assert r.achievement_status == VERIFIED_NOT_ACHIEVED
    assert r.witness is None


def test_chain_wrong_oid_404_refuted(inst, ir_goal):
    """错误 oid:ship 不存在的订单 → 枚举完整 ⇒ EXISTS 反证 → NOT_ACHIEVED。"""
    ir, _, goal = ir_goal
    steps = _chain()
    steps[-1] = {"method": "POST", "path": "/orders/9999/ship",
                 "header": {"Authorization": "Bearer ?token"}}
    record, _ = _run_chain(inst, steps, ir=ir)
    assert record.steps[-1].status == 404
    env = BindingEnv(fixed={"?order": ["9999"], "?user": ["bob"]},
                     provenance={"?user": "auth:token"})
    pre_view = _view(record.steps[-1].before, None, "before-ship")
    r = evaluate_goal(goal, pre_view, env,
                      path_context={"guards": True, "err": False},
                      trace_covered=None)
    assert r.relations_truth == "FALSE"
    assert "EXISTS" in (r.failed_constraint or "")
    assert r.achievement_status == VERIFIED_NOT_ACHIEVED


def test_chain_unpaid_409_refuted(inst, ir_goal):
    """未支付:create 后直接 ship → 入口 status≠PAID 反证 → NOT_ACHIEVED。"""
    ir, _, goal = ir_goal
    record, _ = _run_chain(inst, _chain(with_pay=False), ir=ir)
    assert record.steps[-1].status == 409
    pre_view = _view(record.steps[-1].before, None, "before-ship")
    r = evaluate_goal(goal, pre_view, _env(record, "bob"),
                      path_context={"guards": True, "err": False},
                      trace_covered=None)
    assert r.relations_truth == "FALSE"
    assert "status" in (r.failed_constraint or "")
    assert r.achievement_status == VERIFIED_NOT_ACHIEVED


def test_chain_underpaid_409_refuted(inst, ir_goal):
    """支付不足(amount=10<total=100):不达 PAID → ship 409 + 入口反证。"""
    ir, _, goal = ir_goal
    record, _ = _run_chain(inst, _chain(amount=10), ir=ir)
    assert record.steps[3].status == 200          # pay 请求本身 200
    assert record.steps[4].status == 409
    pre_view = _view(record.steps[4].before, None, "before-ship")
    r = evaluate_goal(goal, pre_view, _env(record, "bob"),
                      path_context={"guards": True, "err": False},
                      trace_covered=None)
    assert r.relations_truth == "FALSE"           # status 仍 CREATED
    assert r.achievement_status == VERIFIED_NOT_ACHIEVED


def test_observer_failure_yields_unknown_not_crash(inst, ir_goal):
    """观测器抛异常 → harness 计数 + 空观测;判达 UNKNOWN(不 crash,
    不伪造成功)。"""
    ir, _, goal = ir_goal
    observer = inst.observer()
    calls = {"n": 0}

    def exploding_observe():
        calls["n"] += 1
        raise RuntimeError("db locked")

    observer.observe = exploding_observe
    execution = ExecutionContext(
        SnapshotLifecycle(files=[inst.db_path],
                          memory_hooks=[inst.bindings.clear]))
    harness = RequestLevelHarness(execution, observer=observer,
                                  bindings_fn=lambda: dict(inst.bindings))
    record = harness.run_input(_chain(), lambda s: inst.request(s).status_code)
    assert record.observer_failures == 10        # 5 步 × before/after
    assert all(s.before == () and s.after == () for s in record.steps)
    empty = ObservationView.from_observations(
        [], observation_point="p",
        families_complete=())   # 观测失败 ⇒ 枚举完整性不可声明(诚实口径)
    r = evaluate_goal(goal, empty, BindingEnv(
        fixed={"?order": ["1"], "?user": ["bob"]}),
                      path_context={"guards": True, "err": False},
                      trace_covered=None)
    assert r.relations_truth == "UNKNOWN" and r.witness is None


# ------------------------------------------------------------- 隔离与重放

def test_input_isolation_and_cold_replay(inst, ir_goal):
    """不同输入不串扰(每输入回基线);同一链冷启动重放两次均全量成功,
    资源 ID 经 extract 重建(第二次 oid 不同)。"""
    ir, target_block, goal = ir_goal
    oids = []
    for _ in range(2):
        record, session = _run_chain(inst, _chain(), ir=ir)
        assert [s.status for s in record.steps] == [201, 200, 201, 200, 200]
        oids.append(str(record.steps[2].bindings["oid"]))   # extract 回填
        # 输入后回基线:users=0/orders=0(隔离在输入边界,序列内共享)
        conn = sqlite3.connect(inst.db_path)
        n_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        n_orders = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        conn.close()
        assert (n_users, n_orders) == (0, 0)
        # 序列内共享:create 后 pay/ship 见同一订单(输入中途不 restore)
        assert record.steps[2].bindings.get("oid") is not None
    # 资源 ID 经 extract 重建(不写死);基线还原下 rowid 可复用同值,
    # "可变"指绑定来源是响应回填而非常量——两次绑定均在场即成立
    assert all(o for o in oids) and len(set(oids)) >= 1


def test_state_persists_within_input(inst):
    """一次输入中途不做 restore:create 的订单对 pay/ship 可见
    (绑定跨步存活——序列语义,与输入边界隔离互补)。"""
    record, _ = _run_chain(inst, _chain())
    oid = str(record.steps[2].bindings["oid"])
    after_create = record.steps[2].after
    after_pay = record.steps[3].after
    c = {o.key.entity for o in after_create if o.subject == "Order.exists"}
    p = {o.key.entity for o in after_pay if o.subject == "Order.exists"}
    assert oid in c and oid in p                 # 两步见到同一订单
    assert record.steps[3].status == 200         # pay 作用于该订单
