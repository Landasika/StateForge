"""P0-B 运行时状态观测契约测试 —— 三值证据,防"代理冒充事实"。

覆盖:StateKey 校验/渲染/解析往返(含 @ 实体)、Evidence 三值语义、
台账新颖度((render, value) 去重、值翻转为新证据)、verified/proxy
分桶、payload 往返、坏行拒收、NullObserver 零事实。
"""

from __future__ import annotations

import pytest

from stateforge.state.runtime_observer import (
    Evidence,
    SCOPE_GLOBAL,
    SCOPE_RESOURCE,
    SCOPE_UNKNOWN,
    SCOPE_USER,
    NullObserver,
    StateEvidenceLedger,
    StateKey,
    StateObservation,
    control_flow_proxy_observation,
)


def obs(predicate: str, entity: str = "*", scope: str = SCOPE_GLOBAL,
        value: bool = True, evidence: Evidence = Evidence.RUNTIME_VERIFIED,
        source: str = "test") -> StateObservation:
    return StateObservation(StateKey(predicate, entity, scope), value,
                            evidence, source)


# ------------------------------------------------------------- StateKey

def test_state_key_render_and_parse_roundtrip():
    key = StateKey("Order.status=PAID", "3", SCOPE_RESOURCE)
    rendered = key.render()
    assert rendered == "Order.status=PAID{3@resource}"
    back = StateObservation.from_dict(
        {"key": rendered, "value": True,
         "evidence": "runtime_verified"}).key
    assert (back.predicate, back.entity, back.scope) == \
        ("Order.status=PAID", "3", SCOPE_RESOURCE)
    assert back == key


def test_state_key_entity_may_contain_at_sign():
    """实体开放词汇(邮箱用户名):解析从右切,实体含 @ 不歧义。"""
    key = StateKey("User.exists", "alice@example.com", SCOPE_USER)
    d = StateObservation(key, True, Evidence.RUNTIME_VERIFIED).to_dict()
    back = StateObservation.from_dict(d)
    assert back.key.entity == "alice@example.com"
    assert back.key.scope == SCOPE_USER


def test_state_key_validation():
    with pytest.raises(ValueError):
        StateKey("", "e", SCOPE_USER)               # 谓词非空
    with pytest.raises(ValueError):
        StateKey("P", "", SCOPE_USER)               # 实体非空(全局用 *)
    with pytest.raises(ValueError):
        StateKey("P", "e", "nonsense")              # 作用域封闭词汇
    with pytest.raises(ValueError):
        StateObservation.from_dict(                 # 坏渲染键拒收
            {"key": "no-braces-here", "value": True,
             "evidence": "runtime_verified"})


def test_state_key_distinguishes_entity_and_scope():
    """同一谓词不同实体/作用域 = 不同状态键(不合并)。"""
    a = StateKey("Session.authenticated", "alice", SCOPE_USER)
    b = StateKey("Session.authenticated", "bob", SCOPE_USER)
    c = StateKey("Session.authenticated", "*", SCOPE_GLOBAL)
    assert len({a.render(), b.render(), c.render()}) == 3


# ------------------------------------------------------------- 台账

def test_ledger_novelty_on_key_value_pair():
    led = StateEvidenceLedger("t")
    first = led.record([obs("User.role=admin", "alice", SCOPE_USER)])
    assert len(first) == 1
    assert led.record([obs("User.role=admin", "alice", SCOPE_USER)]) == []
    flip = led.record([obs("User.role=admin", "alice", SCOPE_USER,
                           value=False)])           # 值翻转到未见极性 = 新证据
    assert len(flip) == 1
    assert len(led.verified_keys) == 1              # 键层仍单一
    assert len(led.verified_observations) == 2


def test_ledger_proxy_bucket_separate_from_verified():
    """代理原子与运行时事实分桶:verified_atoms / proxy_atoms 不混。"""
    led = StateEvidenceLedger("t")
    led.record([obs("Order.status=PAID", "7", SCOPE_RESOURCE)])
    led.record_proxy(["Session.role=admin"])
    payload = led.to_payload()
    assert payload["verified_atoms"] == ["Order.status=PAID{7@resource}"]
    assert payload["proxy_atoms"] == ["Session.role=admin{*@unknown}"]


def test_ledger_ignores_non_verified_observations():
    """CONTROL_FLOW_PROXY / UNKNOWN 不入事实桶(经 record 直灌也不行)。"""
    led = StateEvidenceLedger("t")
    proxy = control_flow_proxy_observation("Session.authed_as=root")
    assert proxy.evidence is Evidence.CONTROL_FLOW_PROXY
    assert proxy.key.scope == SCOPE_UNKNOWN and proxy.key.entity == "*"
    unknown = StateObservation(StateKey("P", "*", SCOPE_GLOBAL), True,
                               Evidence.UNKNOWN)
    assert led.record([proxy, unknown]) == []
    assert led.verified_keys == set()


def test_ledger_payload_roundtrip_and_bad_rows():
    led = StateEvidenceLedger("obs")
    led.record([obs("User.exists", "u1", SCOPE_USER),
                obs("Coupon.used", "SAVE10", SCOPE_RESOURCE, value=False)])
    payload = led.to_payload()
    assert payload["evidence_version"] == 1 and payload["observer"] == "obs"

    fresh = StateEvidenceLedger()
    fresh.load_payload(payload)                     # 跨 epoch 结转
    assert fresh.verified_keys == led.verified_keys
    assert fresh.load_payload({"verified": [{"key": "bad", "value": 1,
                                             "evidence": "runtime_verified"}]}
                              ) is None            # 坏行跳过,宁缺毋假
    assert fresh.verified_keys == led.verified_keys


def test_null_observer_emits_nothing():
    """NullObserver = 显式零事实(不是零状态);满足 RuntimeObserver 协议。"""
    o = NullObserver()
    assert o.observe() == []
    assert o.name == "null"


def test_proxy_atoms_do_not_falsify_verified_vocab():
    """语义护栏:代理口径键渲染(scope=unknown)不得与任何 verified 键碰撞。"""
    led = StateEvidenceLedger("t")
    led.record([obs("Session.role=admin", "root", SCOPE_USER)])
    led.record_proxy(["Session.role=admin"])        # 同名代理原子
    payload = led.to_payload()
    assert payload["verified_atoms"] == ["Session.role=admin{root@user}"]
    assert payload["proxy_atoms"] == ["Session.role=admin{*@unknown}"]
    assert not (set(payload["verified_atoms"]) & set(payload["proxy_atoms"]))


# --------------------------------------------------- PR-03:v2 可选列

def test_observation_v2_optional_columns_roundtrip():
    """v2 列(subject/observed_value/value_kind/observed_at)序列化往返;
    旧四字段构造/渲染不变。"""
    key = StateKey("Order.user_id=7", "3", SCOPE_RESOURCE)
    obs = StateObservation(key=key, value=True,
                           evidence=Evidence.RUNTIME_VERIFIED,
                           source="db:orders", subject="Order.user_id",
                           observed_value=7, value_kind="NUMBER",
                           observed_at=3)
    d = obs.to_dict()
    assert d["subject"] == "Order.user_id"
    assert d["observed_value"] == 7 and d["value_kind"] == "NUMBER"
    assert d["observed_at"] == 3
    again = StateObservation.from_dict(d)
    assert again == obs and again.render() == obs.render()
    # 旧形态(无 v2 列):to_dict 不携带新键,from_dict 缺省读
    old = StateObservation(key=StateKey("Coupon.used", "C"),
                           value=False,
                           evidence=Evidence.RUNTIME_VERIFIED)
    d2 = old.to_dict()
    assert set(d2) == {"key", "value", "evidence", "source"}
    assert StateObservation.from_dict(d2) == old


def test_value_kind_vocabulary_and_observability_words():
    from stateforge.state.runtime_observer import (
        NOT_OBSERVABLE, OBSERVABLE, OBSERVABILITY_STATUSES, UNPROBED,
        VALUE_KINDS)
    assert {"ENUM", "BOOLEAN", "NUMBER", "STRING",
            "NULL", "MISSING", "UNKNOWN"} <= VALUE_KINDS
    assert OBSERVABILITY_STATUSES == {OBSERVABLE, NOT_OBSERVABLE, UNPROBED}
