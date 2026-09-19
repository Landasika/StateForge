"""SF6 lifting 单测:mock provider,不发网络。

覆盖:合法标注应用 / 非法输出降级 / relate 解析 / label 全覆盖校验。
"""

import json

import pytest

from stateforge.core.model import StateCandidate
from stateforge.llm import cache
from stateforge.llm.lifting import LiftResult, label, relate
from stateforge.llm.provider import Provider


class FakeProvider(Provider):
    def __init__(self, responses: list[str]):
        super().__init__("anthropic", "http://fake", {}, "fake-model")
        self.responses = responses
        self.calls = 0

    def complete(self, system: str, user: str, temperature: float = 0.0) -> str:
        self.calls += 1
        return self.responses[min(self.calls - 1, len(self.responses) - 1)]


@pytest.fixture(autouse=True)
def _clean_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")


def _cands():
    return [
        StateCandidate(var="Order.status", entity="Order", attr="status",
                       ops=["compare", "write"], evidence='order["status"] != "PAID"',
                       source="condition", file="app.py", line=1),
        StateCandidate(var="Coupon.used", entity="Coupon", attr="used",
                       ops=["compare", "write"], evidence='if coupon["used"]:',
                       source="condition", file="app.py", line=2),
    ]


def test_label_llm_applies_enum_labels():
    resp = json.dumps([
        {"var": "Order.status", "label": "workflow", "rationale": "订单生命周期", "confidence": 0.9},
        {"var": "Coupon.used", "label": "single_use", "rationale": "单次券", "confidence": 0.95},
    ])
    p = FakeProvider([resp])
    svs, lift = label(_cands(), {"Order.status": ["CREATED", "PAID"]}, provider=p)
    assert lift.source.startswith("llm:")
    by = {sv.var: sv for sv in svs}
    assert by["Order.status"].label == "workflow"
    assert "[llm:fake-model]" in by["Order.status"].rationale
    assert len(lift.label_log) == 2


def test_label_invalid_json_falls_back_to_rules():
    p = FakeProvider(["这不是 JSON", "仍然不是"])
    svs, lift = label(_cands(), {}, provider=p)
    assert lift.source == "heuristic"
    assert lift.meta.get("fallback") == "schema_violation"
    by = {sv.var: sv for sv in svs}
    assert by["Coupon.used"].label == "single_use"  # 规则兜底仍工作


def test_label_rejects_missing_candidates():
    """缺候选 = 全量校验失败 → 降级(防 LLM 丢项)。"""
    resp = json.dumps([{"var": "Order.status", "label": "workflow", "rationale": "x"}])
    p = FakeProvider([resp, resp])
    _, lift = label(_cands(), {}, provider=p)
    assert lift.source == "heuristic"


def test_relate_parses_hierarchy():
    resp = json.dumps({
        "role_hierarchy": {"admin": ["user"]},
        "extra_transitions": [{"endpoint": "POST /orders", "from": "CREATED", "to": "PAID", "var": "Order.status"}],
        "cross_entity_invariants": [{"vars": ["Order.status", "Order.paid_amount"], "rule": "SHIPPED ⇒ paid≥total"}],
    })
    p = FakeProvider([resp])
    from stateforge.core.model import StateVar
    r = relate([StateVar(entity="User", attr="role", label="authz")], [], provider=p)
    assert isinstance(r, LiftResult)
    assert r.role_hierarchy == {"admin": ["user"]}
    assert r.cross_entity_invariants[0].rule.startswith("SHIPPED")


def test_provider_none_means_rules():
    svs, lift = label(_cands(), {}, provider=None)
    assert lift.source == "heuristic"
    assert svs
