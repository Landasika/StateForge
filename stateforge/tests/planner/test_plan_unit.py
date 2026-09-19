"""SF3-T2 单测:合成图反向规划 + vulnshop 五 goal 链形(纯驱动侧,不起服务)。"""

from pathlib import Path

import networkx as nx
import pytest

from stateforge.analyzer.analyzer import analyze
from stateforge.graph.builder import build_sstg
from stateforge.planner.backward import plan_backward
from stateforge.planner.goals import GoalSpec, VULNSHOP_GOALS

VULNSHOP = Path(__file__).parents[2] / "targets" / "vulnshop"


@pytest.fixture(scope="module")
def sstg_model():
    m = analyze(VULNSHOP, "flask")
    return m, build_sstg(m)


def chains(model, graph):
    return {g.name: [s.action for s in plan_backward(model, graph, g).steps]
            for g in VULNSHOP_GOALS}


def test_workflow_bypass_chain_skips_pay(sstg_model):
    m, g = sstg_model
    chain = chains(m, g)["goal_workflow_bypass"]
    assert chain == ["POST /auth/register", "POST /auth/login",
                     "POST /orders", "POST /orders/<int:oid>/ship"]
    assert "pay" not in " ".join(chain)  # 攻击假设:跳过支付直达 SHIPPED


def test_payment_chain_includes_pay(sstg_model):
    m, g = sstg_model
    chain = chains(m, g)["goal_payment_bypass"]
    assert chain[-1] == "POST /orders/<int:oid>/pay"
    assert "POST /orders" in chain


def test_idor_two_actors(sstg_model):
    m, g = sstg_model
    chain = chains(m, g)["goal_idor"]
    assert chain.count("POST /auth/register") == 2  # A 与 B 独立身份
    assert chain[-1] == "GET /orders/<int:oid>"


def test_privilege_chain(sstg_model):
    m, g = sstg_model
    chain = chains(m, g)["goal_privilege"]
    assert chain == ["POST /auth/register", "POST /auth/login",
                     "GET /auth/me", "POST /admin/users/<int:uid>/role"]


def test_synthetic_graph_planning():
    """5 节点合成图:登录→建单→支付→发货 的依赖序与深度上限。"""
    g = nx.MultiDiGraph()
    for n in ("Session.authed_as=<authed>", "Order.status=CREATED", "Order.status=PAID"):
        g.add_node(n, kind="state", var=n.split("=")[0], value="x")
    for n in ("POST /login", "POST /orders", "POST /orders/<int:oid>/pay"):
        g.add_node(n, kind="action")
    g.add_edge("POST /login", "Session.authed_as=<authed>", kind="produces")
    g.add_edge("POST /orders", "Order.status=CREATED", kind="produces")
    g.add_edge("POST /orders", "Session.authed_as=<authed>", kind="requires")
    g.add_edge("POST /orders/<int:oid>/pay", "Order.status=PAID", kind="produces")
    g.add_edge("POST /orders/<int:oid>/pay", "Session.authed_as=<authed>", kind="requires")
    g.add_edge("POST /orders/<int:oid>/pay", "Order.status=CREATED", kind="requires")

    goal = GoalSpec(name="synthetic", vuln_class="x", kind="reach_state",
                    target="Order.status=PAID", max_depth=4)
    from stateforge.core.model import AppModel
    seg = plan_backward(AppModel(app_root="synthetic", framework="flask"), g, goal)
    actions = [s.action for s in seg.steps]
    assert actions == ["POST /login", "POST /orders", "POST /orders/<int:oid>/pay"]
