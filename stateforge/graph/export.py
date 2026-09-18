"""export.py —— SSTG 导出:stategraph.json(node_link)+ dot(security_relevant 高亮)。"""

from __future__ import annotations

import json
from pathlib import Path

import networkx as nx


def to_json(g: nx.MultiDiGraph, path: str | Path) -> None:
    data = nx.node_link_data(g, edges="edges")
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def to_dot(g: nx.MultiDiGraph, path: str | Path) -> None:
    lines = ["digraph SSTG {", "  rankdir=LR;", '  node [fontname="Helvetica"];']
    for n, d in g.nodes(data=True):
        if d.get("kind") == "state":
            style = ", style=bold, color=red" if d.get("security_relevant") else ""
            lines.append(f'  "{n}" [shape=ellipse{style}];')
        else:
            lines.append(f'  "{n}" [shape=box];')
    for u, v, k, d in g.edges(keys=True, data=True):
        kind = d.get("kind", "")
        attr = ""
        if kind == "requires":
            attr = ' [style=dashed, label="requires"]'
        elif kind == "produces":
            attr = ' [label="produces"]'
        elif kind == "invariant":
            attr = (' [style=dotted, color=green, label="legal"]' if d.get("legal")
                    else ' [style=dotted, color=red, label="ILLEGAL"]')
        lines.append(f'  "{u}" -> "{v}"{attr};')
    lines.append("}")
    Path(path).write_text("\n".join(lines), encoding="utf-8")
