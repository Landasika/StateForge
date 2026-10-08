"""SF1-T6:SSTG 构建冒烟(vulnshop)。"""

from pathlib import Path

import networkx as nx
import pytest

from stateforge.analysis.analyzer import analyze
from stateforge.analysis.builder import build_sstg
from stateforge.analysis.export import to_dot, to_json

VULNSHOP = Path(__file__).parents[2] / "targets" / "vulnshop"


@pytest.fixture(scope="module")
def sstg() -> nx.MultiDiGraph:
    return build_sstg(analyze(VULNSHOP, "flask"))


def test_bipartite_nodes(sstg):
    states = [n for n, d in sstg.nodes(data=True) if d["kind"] == "state"]
    actions = [n for n, d in sstg.nodes(data=True) if d["kind"] == "action"]
    assert "Order.status=SHIPPED" in states
    assert "Session.authed_as=<authed>" in states
    assert any("ship" in a for a in actions)


def test_requires_and_produces(sstg):
    ship = next(n for n, d in sstg.nodes(data=True) if d.get("kind") == "action" and "ship" in n)
    requires = {v for _, v, d in sstg.edges(ship, data=True) if d["kind"] == "requires"}
    produces = {v for _, v, d in sstg.edges(ship, data=True) if d["kind"] == "produces"}
    assert "Order.status=PAID" in requires  # guard-return 归一
    assert "Session.authed_as=<authed>" in requires
    assert "Order.status=SHIPPED" in produces


def test_invariant_edges(sstg):
    a, b = "Order.status=CREATED", "Order.status=SHIPPED"
    edges = list(sstg.get_edge_data(a, b, default={}).values())
    assert any(d.get("kind") == "invariant" and d.get("legal") is False for d in edges)
    legal = list(sstg.get_edge_data("Order.status=PAID", "Order.status=SHIPPED", default={}).values())
    assert any(d.get("legal") is True for d in legal)


def test_export(tmp_path, sstg):
    to_json(sstg, tmp_path / "stategraph.json")
    to_dot(sstg, tmp_path / "stategraph.dot")
    assert (tmp_path / "stategraph.json").stat().st_size > 0
    assert "digraph SSTG" in (tmp_path / "stategraph.dot").read_text()
