"""诊断 1 回归:多生产者择优(不再盲取 producers[0])。

同原子有两个 produces 边生产者时,选未满足前置更少者(步数=预算);
平手时总前置少者;再平手按字典序(确定性,配对实验可复现)。
soft 兜底路径不参与该排序(域序 item 级优先,见 test_plan_unit)。
"""

import networkx as nx

from stateforge.core.model import AppModel
from stateforge.state.planning.backward import plan_backward
from stateforge.state.planning.goals import GoalSpec


def _sstg() -> nx.MultiDiGraph:
    g = nx.MultiDiGraph()
    for n in ("Session.authed_as=<authed>", "Order.status=CREATED",
              "Order.status=PAID"):
        g.add_node(n, kind="state", var=n.split("=")[0], value="x")
    for n in ("POST /expensive", "POST /cheap", "POST /login",
              "POST /orders"):
        g.add_node(n, kind="action")
    # 两生产者都产 PAID;expensive 未满足前置 2(authed+CREATED),
    # cheap 仅 1(authed)——注释里故意让 expensive 先加边,验证非首拣
    g.add_edge("POST /expensive", "Order.status=PAID", kind="produces")
    g.add_edge("POST /expensive", "Session.authed_as=<authed>", kind="requires")
    g.add_edge("POST /expensive", "Order.status=CREATED", kind="requires")
    g.add_edge("POST /cheap", "Order.status=PAID", kind="produces")
    g.add_edge("POST /cheap", "Session.authed_as=<authed>", kind="requires")
    g.add_edge("POST /login", "Session.authed_as=<authed>", kind="produces")
    g.add_edge("POST /orders", "Order.status=CREATED", kind="produces")
    g.add_edge("POST /orders", "Session.authed_as=<authed>", kind="requires")
    return g


def _plan(g, **kw):
    goal = GoalSpec(name="syn", vuln_class="x", kind="reach_state",
                    target="Order.status=PAID", max_depth=4, **kw)
    return plan_backward(AppModel(app_root="syn", framework="flask"), g, goal)


def test_cheaper_producer_wins_over_first_edge():
    seg = _plan(_sstg())
    actions = [s.action for s in seg.steps]
    assert "POST /cheap" in actions
    assert "POST /expensive" not in actions
    # cheap 的前置链照常补齐
    assert "POST /login" in actions


def test_tiebreak_deterministic():
    """两生产者成本完全一致 → 字典序稳定(可复现)。"""
    g = _sstg()
    g.add_node("POST /aaa", kind="action")
    g.add_edge("POST /aaa", "Order.status=PAID", kind="produces")
    g.add_edge("POST /aaa", "Session.authed_as=<authed>", kind="requires")
    seg = _plan(g)
    actions = [s.action for s in seg.steps]
    assert "POST /aaa" in actions  # aaa 与 cheap 同成本,字典序胜出
    assert "POST /cheap" not in actions
