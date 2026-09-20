"""SF-EXT(T+11):过程内 CFG + 执行流图 G_xfg(第一步:静态控制流图获取)。

先于实现的规格:
  build_cfg(func)   语句序列 → 基本块图(顺序/分支/循环/异常/终止边)
  attach_anchors    端点事实(Effect/Precondition/Guard,带 file:line)钉到块
  build_xfg         端点节点 + 各自 CFG 子图 + 跨端点共享状态虚边 = G_xfg
"""

import ast
from pathlib import Path

import networkx as nx
import pytest

from stateforge.analyzer.analyzer import analyze
from stateforge.analyzer.cfg import attach_anchors, build_cfg, build_xfg

SRC = """
def f(x):
    a = 1
    if x > 0:
        a = 2
    else:
        a = 3
    return a
"""


def _cfg_of(src: str, func: str = "f") -> nx.DiGraph:
    tree = ast.parse(src)
    fdef = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == func)
    return build_cfg(fdef, file="<test>")


def test_if_else_shape():
    g = _cfg_of(SRC)
    branch = [n for n, d in g.nodes(data=True) if d.get("kind") == "branch"]
    assert len(branch) == 1
    succ = g.edges(branch[0], data=True)
    conds = {d.get("cond") for _, _, d in succ}
    assert conds == {"T", "F"}
    # 两个分支块都汇入含 return 的块
    ret = [n for n, d in g.nodes(data=True) if d.get("terminal")]
    assert ret and all(g.has_edge(b, ret[0]) for b in
                       [v for u, v, d in succ if d["cond"] in ("T", "F")])


def test_while_back_edge():
    src = "def f(n):\n    i = 0\n    while i < n:\n        i += 1\n    return i\n"
    g = _cfg_of(src)
    branch = next(n for n, d in g.nodes(data=True) if d.get("kind") == "branch")
    body = [v for u, v, d in g.edges(branch, data=True) if d.get("cond") == "T"][0]
    assert g.has_edge(body, branch)  # 回边


def test_return_terminates():
    g = _cfg_of(SRC)
    terminals = [n for n, d in g.nodes(data=True) if d.get("terminal")]
    assert len(terminals) == 1


VULNSHOP = Path(__file__).parents[2] / "targets" / "vulnshop"


@pytest.fixture(scope="module")
def model():
    return analyze(VULNSHOP, "flask")


def _ship_endpoint(m):
    return next(ep for ep in m.endpoints if "ship" in ep.route)


def test_endpoint_cfg_branch_and_anchor(model):
    ep = _ship_endpoint(model)
    g = build_cfg(ep)
    assert g.number_of_nodes() >= 3
    # ship 的 PAID 前置(Precondition Order.status)钉在某个分支块上
    pre = next(p for p in ep.preconditions if p.var == "Order.status")
    anchored = attach_anchors(g, ep)
    anchor_blocks = [n for n, d in anchored.nodes(data=True)
                     if any(a["kind"] == "pre" and a["var"] == "Order.status"
                            for a in d.get("anchors", []))]
    assert anchor_blocks, "PAID 前置未锚到任何块"
    # 锚点行号落在块的行范围内
    d = anchored.nodes[anchor_blocks[0]]
    lo, hi = d["lines"]
    assert lo <= pre.line <= hi


def test_xfg_assembly(model):
    xfg = build_xfg(model)
    actions = [n for n, d in xfg.nodes(data=True) if d.get("kind") == "action"]
    blocks = [n for n, d in xfg.nodes(data=True) if d.get("kind") == "block"]
    assert len(actions) == 11
    assert len(blocks) >= 30                     # 11 个端点的块总和
    ship = next(n for n in actions if "ship" in n)
    ship_blocks = [v for u, v in xfg.edges(ship)]  # action → 入口块边
    assert ship_blocks
    # 跨端点共享状态虚边:ship 的 status 读锚 ↔ pay/create 的写锚
    state_links = [e for e in xfg.edges(data=True) if e[2].get("kind") == "state-link"]
    assert state_links


def test_xfg_export_dot(tmp_path, model):
    from stateforge.analyzer.cfg import xfg_to_dot

    xfg = build_xfg(model)
    out = tmp_path / "xfg.dot"
    xfg_to_dot(xfg, out)
    text = out.read_text()
    assert "digraph XFG" in text and "ship" in text


# ---------------------------------------------------------------- v2:基于调用图


def test_xfg_on_callgraph_expands_helpers(model):
    """基于②(调用图)添加①:项目内函数(含非端点 helper)全部展开为 CFG。"""
    xfg = build_xfg(model, callgraph_path=VULNSHOP / "golden" / "callgraph.json")
    kinds = {}
    for _n, d in xfg.nodes(data=True):
        kinds[d.get("kind")] = kinds.get(d.get("kind"), 0) + 1
    assert kinds.get("action") == 11
    assert kinds.get("block", 0) + kinds.get("branch", 0) >= kinds.get("block", 0) > 0
    # helper 函数(非端点)也进了图:init_database 的块存在
    helper_blocks = [n for n, d in xfg.nodes(data=True)
                     if "init_database" in str(d.get("func", ""))]
    assert helper_blocks, "helper 函数未展开"


def test_xfg_call_edges_sink_to_blocks(model):
    """② 的调用边下沉到块级:caller 中含调用语句的块 → callee 入口块。"""
    xfg = build_xfg(model, callgraph_path=VULNSHOP / "golden" / "callgraph.json")
    call_edges = [(u, v, d) for u, v, d in xfg.edges(data=True)
                  if d.get("kind") in ("call", "call-ext")]
    assert call_edges, "无调用边"
    # 库函数是叶子(ext):sqlite3/hmac 等不展开
    ext_nodes = [n for n, d in xfg.nodes(data=True) if d.get("kind") == "ext"]
    assert any("sqlite3" in n or "flask" in n for n in ext_nodes)
    # ship 调用了项目内函数 get_order_or_none → call 边指向其入口
    target_entries = [v for u, v, d in call_edges
                      if d["kind"] == "call" and "get_order_or_none" in v]
    assert any(xfg.nodes[v].get("kind") == "entry" for v in target_entries)


def test_xfg_state_links_preserved(model):
    """跨端点共享状态虚边在调用图骨架上仍然保留。"""
    xfg = build_xfg(model, callgraph_path=VULNSHOP / "golden" / "callgraph.json")
    state_links = [e for e in xfg.edges(data=True) if e[2].get("kind") == "state-link"]
    assert state_links
