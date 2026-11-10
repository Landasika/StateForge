"""join + machine 测试:快照差归因、无变化不出边、机器累积与查询。"""

from __future__ import annotations

import textwrap
from pathlib import Path

from stateforge.state.sites import StoreSite, extract_sites
from stateforge.state.join import attribute, diff, observe
from stateforge.state.machine import StateMachine


def _pt(key, op="write", line=16, fn="login_user", chain="session"):
    return StoreSite(chain=chain, key=key, key_status="literal", op=op,
                     value=None, file="/app/CTFd/utils/security/auth.py",
                     line=line, function=fn)


# ---------------------------------------------------------------- diff

def test_diff_added_removed_changed():
    d = diff({"id": 1, "old": 2}, {"id": 2, "new": 3})
    assert d.added == ("new",)
    assert d.removed == ("old",)
    assert d.changed == ("id",)


# ---------------------------------------------------------------- attribute

def test_attribute_exact_underscore_clear():
    executed = [_pt("id"), _pt("hash"), _pt("permanent", line=19),
                _pt(None, op="flush", line=35, fn="logout_user")]
    d = diff({"id": 1, "gone": 1},
             {"id": 2, "_permanent": True})
    at = dict((a.key, a) for a in attribute(d, executed))
    assert at["id"].via == "exact"                     # 值变化,精确
    assert at["_permanent"].via == "underscore"        # 去前导 _ 后命中
    assert at["_permanent"].point.line == 19
    assert at["gone"].via == "clear"                   # 删除归到 flush
    assert at["gone"].point.function == "logout_user"


def test_attribute_unattributed_honest():
    at = attribute(diff({}, {"mystery": 1}), [_pt("id")])
    assert at[0].via == "unattributed" and at[0].point is None


def test_attribute_chain_filter():
    other = _pt("id", chain="db.session")
    at = attribute(diff({}, {"id": 1}), [other], chains={"session"})
    assert at[0].via == "unattributed"                 # 别的 store 不掺和


# ---------------------------------------------------------------- observe

def test_observe_none_when_no_change():
    """登录失败:快照不变 → 不产转移。"""
    snap = {"nonce": "x"}
    assert observe("POST /login", snap, dict(snap), [_pt("id")]) is None


def test_observe_states_are_presence_sets():
    t = observe("POST /login", {"nonce": "a"},
                {"nonce": "b", "id": 1, "hash": "h"},
                [_pt("id"), _pt("nonce", line=17)])
    assert t.from_state == frozenset({"nonce"})
    assert t.to_state == frozenset({"nonce", "id", "hash"})
    assert t.delta.changed == ("nonce",)               # 轮换记入 changed
    assert t.points


# ---------------------------------------------------------------- machine

def _sample_transition(trigger, before, after):
    return observe(trigger, before, after, [_pt("id")])


def test_machine_accumulate_dedupe_and_query():
    m = StateMachine()
    assert m.add(_sample_transition("POST /login", {}, {"id": 1})) is True
    assert m.add(_sample_transition("POST /login", {}, {"id": 2})) is False  # 去重
    assert m.edges[0].hits == 2
    m.add(_sample_transition("GET /logout", {"id": 1}, {}))
    assert len(m.states) == 2                            # {} 和 {id}
    prods = m.triggers_producing("id")
    assert [e.trigger for e in prods] == ["POST /login"]


def test_machine_dot_renders():
    m = StateMachine()
    m.add(_sample_transition("POST /login", {}, {"id": 1}))
    dot = m.to_dot()
    assert "POST /login" in dot and "∅" in dot and "{id}" in dot
