"""Static+dynamic coverage overlay as a Graphviz dot file (spec P1-5)."""

from __future__ import annotations

from pathlib import Path

from stateforge.core.ir import ProgramIR

_KIND_SHAPE = {"branch": "diamond", "entry": "circle", "exit": "doublecircle",
               "terminal": "box", "callsite": "point",
               "returnsite": "point", "loop_exit": "point", "try": "ellipse"}


def coverage_dot(ir: ProgramIR, feedback: dict, path: str | Path) -> Path:
    """Render ICFG with dynamic coverage and frontier highlights.

    ``feedback`` is the payload produced by ``cli.py feedback`` (or any dict
    with covered_blocks / frontiers / block_hits keys).
    """
    hits = feedback.get("block_hits", {})
    covered = set(feedback.get("covered_blocks", []))
    frontier = {(f["src"], f["dst"]) for f in feedback.get("frontiers", [])}
    lines = ["digraph Coverage {", "  rankdir=LR;", '  node [fontname="Helvetica"];']
    for bid, block in ir.blocks.items():
        shape = _KIND_SHAPE.get(block.kind, "rect")
        if bid in covered:
            lines.append(f'  "{bid}" [shape={shape}, style=filled, '
                         f'fillcolor="palegreen", label="{bid}\\n hits={hits.get(bid, 0)}"];')
        else:
            lines.append(f'  "{bid}" [shape={shape}, color="gray", '
                         f'fontcolor="gray", label="{bid}"];')
    for edge in ir.edges:
        if edge.kind not in {"flow", "true", "false", "call", "return"}:
            continue
        if (edge.src, edge.dst) in frontier:
            lines.append(f'  "{edge.src}" -> "{edge.dst}" '
                         '[color="red", penwidth=2.5, label="frontier"];')
        else:
            # 属性必须整体放进一个方括号组(color 裸写在外面是 dot 语法错误)
            attrs = []
            if edge.kind in ("call", "return"):
                attrs.append(f'label="{edge.kind}"')
                attrs.append('color="purple"')
            attr = f" [{', '.join(attrs)}]" if attrs else ""
            lines.append(f'  "{edge.src}" -> "{edge.dst}"{attr};')
    lines.append("}")
    out = Path(path)
    out.write_text("\n".join(lines), encoding="utf-8")
    return out
