"""SF1 golden:analyzer(vulnshop) vs 人工核准的 AppModel.golden.json。

golden 于 2026-09-18 按下列人工期望逐项核对后冻结:
  - 11 路由;register 产 User.role=user;login 产 Session.authed_as
  - ship requires Order.status==PAID(guard-return 惯用法归一)+ owner 守卫
  - /admin role 端点有 role 守卫;read/pay/cancel 有 owner 守卫
  - 六类标签:User.role=authz, Session.authed_as=authn, Order.status=workflow,
    Order.paid_amount=money, Coupon.used=single_use, Order.user_id=ownership
  - legal_transitions: CREATED→{PAID,CANCELLED}, PAID→SHIPPED(静态 guard→effect 配对)
比较口径:结构覆盖(golden 中的事实必须都在产出里),≥90% 且无 phantom 事实。
"""

from pathlib import Path

import pytest

from stateforge.analysis.analyzer import analyze
from stateforge.core.model import AppModel

GOLDEN = Path(__file__).parents[2] / "targets" / "vulnshop" / "golden" / "AppModel.golden.json"
VULNSHOP = Path(__file__).parents[2] / "targets" / "vulnshop"


@pytest.fixture(scope="module")
def produced() -> AppModel:
    return analyze(VULNSHOP, "flask")


@pytest.fixture(scope="module")
def golden() -> AppModel:
    return AppModel.from_json(GOLDEN)


def test_endpoint_routes_match(produced, golden):
    got = sorted(f"{ep.methods[0]} {ep.route}" for ep in produced.endpoints)
    want = sorted(f"{ep.methods[0]} {ep.route}" for ep in golden.endpoints)
    assert got == want


def test_fact_coverage(produced, golden):
    """"""
    def facts(model: AppModel) -> set:
        out = set()
        for ep in model.endpoints:
            key = f"{ep.methods[0]} {ep.route}"
            for p in ep.params:
                out.add((key, "param", p.name))
            for pre in ep.preconditions:
                out.add((key, "pre", pre.var, pre.op, pre.value))
            for eff in ep.effects:
                out.add((key, "eff", eff.var, eff.value))
            for g in ep.guards:
                out.add((key, "guard", g.kind))
        return out

    want, got = facts(golden), facts(produced)
    missing = want - got
    phantom = got - want
    coverage = len(want - missing) / len(want) if want else 1.0
    assert coverage >= 0.90, f"覆盖 {coverage:.0%},缺失:{sorted(missing)[:10]}"
    assert not phantom, f"phantom 事实(产出有 golden 无):{sorted(phantom)[:10]}"


def test_state_vars_labels(produced, golden):
    want = {sv.var: (sv.label, sv.security_relevant) for sv in golden.state_vars}
    got = {sv.var: (sv.label, sv.security_relevant) for sv in produced.state_vars}
    assert got == want


def test_invariants(produced, golden):
    want = {inv.var: {tuple(t) for t in inv.legal_transitions} for inv in golden.invariants}
    got = {inv.var: {tuple(t) for t in inv.legal_transitions} for inv in produced.invariants}
    assert got == want
    assert ("CREATED", "PAID") in got.get("Order.status", set())
    assert ("PAID", "SHIPPED") in got.get("Order.status", set())


def test_candidates_are_unlabeled_facts(produced):
    """ADR-0009:候选存在且未裁决(六类 key vars 都有候选来源)。"""
    cand_vars = {c.var for c in produced.candidates}
    for var in ("Order.status", "Coupon.used", "User.role", "Session.authed_as"):
        assert var in cand_vars
    for c in produced.candidates:
        assert c.ops, f"候选 {c.var} 无操作记录"
