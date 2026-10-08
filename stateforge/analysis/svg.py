"""svg.py —— 零依赖图渲染(本机无 graphviz):过程内 CFG 与函数级 XFG 出 SVG。"""

from __future__ import annotations

import html
from pathlib import Path

import networkx as nx

_W, _H = 190, 46          # 节点盒
_GAP_X, _GAP_Y = 60, 70   # 层内/层间间距


def _layers(g: nx.DiGraph, entry) -> dict[str, int]:
    """按最长路径分层(忽略回边),简单可靠。"""
    depth: dict[str, int] = {n: 0 for n in g.nodes}

    def visit(n, d, seen):
        if d > depth.get(n, 0):
            depth[n] = d
        for s in g.successors(n):
            if (n, s) not in seen and depth.get(s, 0) <= d + 1:
                visit(s, d + 1, seen | {(n, s)})

    visit(entry, 0, set())
    return depth


def _layout(g: nx.DiGraph, entry):
    depth = _layers(g, entry)
    by_layer: dict[int, list] = {}
    for n, d in depth.items():
        by_layer.setdefault(d, []).append(n)
    pos = {}
    for d, nodes in sorted(by_layer.items()):
        for i, n in enumerate(nodes):
            pos[n] = (i * (_W + _GAP_X), d * (_H + _GAP_Y))
    return pos, max((d for d in by_layer), default=0)


def _esc(s: str) -> str:
    return html.escape(str(s), quote=True)


KIND_STYLE = {
    "entry": ("#cfe8ff", "#4a86c8"),
    "exit": ("#eeeeee", "#999999"),
    "block": ("#ffffff", "#666666"),
    "branch": ("#fff3cc", "#c9a227"),
    "terminal": ("#f3e6ff", "#8e6bbf"),
    "missing": ("#ffdddd", "#cc4444"),
    "action": ("#d6f5d6", "#2e8b57"),
    "ext": ("#f0f0f0", "#bbbbbb"),
    "func": ("#ffffff", "#5577cc"),
}


def render_graph_svg(g: nx.DiGraph, path: str | Path, title: str = "",
                     entry=None, node_label=None, sub_label=None,
                     edge_label=None) -> None:
    """通用有向图 → SVG(层式布局)。node_label/sub_label: (n, data) -> str。"""
    nodes = [n for n in g.nodes if g.nodes[n].get("kind") != "missing"]
    gg = g.subgraph(nodes)
    entry = entry or next((n for n in nodes if g.nodes[n].get("kind") == "entry"), nodes[0] if nodes else None)
    if entry is None:
        Path(path).write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>", encoding="utf-8")
        return
    pos, max_d = _layout(gg, entry)
    xs = [p[0] for p in pos.values()] or [0]
    width, height = max(xs) + _W + 40, (max_d + 1) * (_H + _GAP_Y) + 70

    out = [f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' "
           f"font-family='Helvetica,Arial,sans-serif'>",
           f"<text x='20' y='28' font-size='17' font-weight='bold'>{_esc(title)}</text>",
           "<defs><marker id='a' markerWidth='9' markerHeight='9' refX='8' refY='3' "
           "orient='auto'><path d='M0,0 L9,3 L0,6 z' fill='#555'/></marker></defs>"]

    for n, (x, y) in pos.items():
        d = gg.nodes[n]
        kind = d.get("kind", "block")
        fill, stroke = KIND_STYLE.get(kind, KIND_STYLE["block"])
        anchor = d.get("anchors") or []
        if anchor:
            stroke, sw = "#cc2222", "2.5"        # 状态锚点:红框加粗
        else:
            sw = "1.2"
        out.append(f"<rect x='{x}' y='{y + 40}' width='{_W}' height='{_H}' rx='7' "
                   f"fill='{fill}' stroke='{stroke}' stroke-width='{sw}'/>")
        label = node_label(n, d) if node_label else str(n)
        out.append(f"<text x='{x + 10}' y='{y + 58}' font-size='12' "
                   f"fill='#111'>{_esc(label)[:34]}</text>")
        sub = sub_label(n, d) if sub_label else ""
        if sub:
            out.append(f"<text x='{x + 10}' y='{y + 74}' font-size='10' "
                       f"fill='#a33'>{_esc(sub)[:42]}</text>")

    for item in gg.edges(data=True):
        u, v, d = item[0], item[1], item[-1]
        if u not in pos or v not in pos:
            continue
        (x1, y1), (x2, y2) = pos[u], pos[v]
        sx, sy = x1 + _W / 2, y1 + 40 + _H
        tx, ty = x2 + _W / 2, y2 + 40
        color = {"cfg": "#555", "call": "#7b2fbf", "call-ext": "#aaa",
                 "state-link": "#2b6cb0", "entry-edge": "#2e8b57"}.get(d.get("kind"), "#555")
        dash = " stroke-dasharray='5,4'" if d.get("kind") == "state-link" else ""
        if abs(sx - tx) < 5 and sy < ty:
            out.append(f"<line x1='{sx}' y1='{sy}' x2='{tx}' y2='{ty}' "
                       f"stroke='{color}' marker-end='url(#a)' stroke-width='1.4'{dash}/>")
        else:
            mx = (sx + tx) / 2 + (40 if tx < sx else -40)
            out.append(f"<path d='M{sx},{sy} C{mx},{sy} {mx},{ty} {tx},{ty}' "
                       f"fill='none' stroke='{color}' marker-end='url(#a)' "
                       f"stroke-width='1.4'{dash}/>")
        lab = edge_label(u, v, d) if edge_label else d.get("cond")
        if lab:
            out.append(f"<text x='{(sx + tx) / 2}' y='{(sy + ty) / 2}' font-size='10' "
                       f"fill='{color}'>{_esc(str(lab))[:14]}</text>")
    out.append("</svg>")
    Path(path).write_text("\n".join(out), encoding="utf-8")


def render_function_xfg(xfg: nx.MultiDiGraph, path: str | Path, title: str = "XFG (function-level)") -> None:
    """函数级 XFG:每函数折叠为单节点;call/state-link/entry 边聚合计数。"""
    import collections

    fg = nx.MultiDiGraph()
    for n, d in xfg.nodes(data=True):
        fid = d.get("func_id")
        if fid:
            if fid not in fg:
                fg.add_node(fid, kind="func",
                            anchors=fg.nodes[fid].get("anchors", []) if fg.has_node(fid) else [])
        elif d.get("kind") in ("action", "ext"):
            fg.add_node(n, kind=d["kind"])
    # 聚合边
    counts = collections.Counter()
    for u, v, d in xfg.edges(data=True):
        fu = xfg.nodes[u].get("func_id", u)
        fv = xfg.nodes[v].get("func_id", v)
        if fu != fv:
            counts[(fu, fv, d.get("kind"))] += 1
    for (u, v, kind), c in counts.items():
        if u in fg and v in fg:
            fg.add_edge(u, v, kind=kind, n=c)
    entry = next((n for n in fg if fg.nodes[n].get("kind") == "action"), None)

    def label(n, d):
        return n.split(".")[-1] if d.get("kind") == "func" else n

    def sub(n, d):
        return {"action": "端点动作", "ext": "库函数(叶)"}.get(d.get("kind"), "项目函数")

    def elab(u, v, d):
        return {"call": f"call×{d['n']}", "call-ext": f"ext×{d['n']}",
                "state-link": f"state×{d['n']}", "entry-edge": "entry"}.get(d.get("kind"))

    render_graph_svg(fg, path, title, entry=entry, node_label=label,
                     sub_label=sub, edge_label=elab)
