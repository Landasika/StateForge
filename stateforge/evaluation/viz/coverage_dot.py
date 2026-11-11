"""Static+dynamic coverage overlay as a Graphviz dot file (spec P1-5).

Three kinds of markup share one picture, each from a different source:

* **filled green** -- the block ran during the trace (``block_hits``).
* **blue dashed edge** -- an edge the static analysis did not have and the
  trace supplied (``learned_edges``, as ``(src, dst)`` pairs).
* **thick orange border** -- the block reads or writes session state
  (``session_blocks``, from ``state.session_access.SessionAccess.block_id``).

The last two are deliberately orthogonal to the first: a block can be "has
session access" and "not covered" at the same time, and that combination is
the interesting one -- state changes the fuzzer has never reached.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from stateforge.core.ir import ProgramIR

_KIND_SHAPE = {"branch": "diamond", "entry": "circle", "exit": "doublecircle",
               "terminal": "box", "callsite": "point",
               "returnsite": "point", "loop_exit": "point", "try": "ellipse"}


def _focus_blocks(ir: ProgramIR, *, function: str | None,
                  around: str | None) -> set[str] | None:
    """Blocks to keep, or ``None`` for the whole graph.

    ``block.function`` holds the short name ("follow"), not the qualified one,
    so both spellings are accepted.
    """
    if around is not None:
        keep = {around}
        for edge in ir.edges:
            if edge.src == around or edge.dst == around:
                keep.add(edge.src)
                keep.add(edge.dst)
        return keep
    if function is not None:
        tail = function.rsplit(".", 1)[-1]
        return {bid for bid, block in ir.blocks.items()
                if block.function in (function, tail)}
    return None


def coverage_dot(ir: ProgramIR, feedback: dict, path: str | Path, *,
                 session_blocks: Iterable[str] = (),
                 learned_edges: Iterable[tuple[str, str]] = (),
                 function: str | None = None,
                 around: str | None = None,
                 only_learned: bool = False) -> Path:
    """Render ICFG with coverage, session and learned-edge markup.

    ``feedback`` is the payload produced by ``cli.py feedback`` (or any dict
    with covered_blocks / frontiers / block_hits keys).

    ``function`` limits the picture to one function; ``around`` to one block
    plus its immediate neighbours; ``only_learned`` to the blocks touched by
    learned edges. A whole-program graph of a real target is thousands of
    nodes and reads as a hairball, so these are what make it legible.
    """
    hits = feedback.get("block_hits", {})
    covered = set(feedback.get("covered_blocks", []))
    frontier = {(f["src"], f["dst"]) for f in feedback.get("frontiers", [])}
    session = set(session_blocks)
    learned = set(learned_edges)

    keep = _focus_blocks(ir, function=function, around=around)
    if only_learned:
        keep = set()
        for src, dst in learned:
            keep.add(src)
            keep.add(dst)

    lines = ["digraph Coverage {", "  rankdir=LR;",
             '  node [fontname="Helvetica", fontsize=9];',
             '  edge [fontname="Helvetica", fontsize=7];',
             '  subgraph cluster_legend { label="legend"; style=dashed;',
             '    L1 [label="covered", shape=box, style=filled, fillcolor="palegreen"];',
             '    L2 [label="not covered", shape=box, color="gray", fontcolor="gray"];',
             '    L3 [label="session access", shape=box, color="darkorange", penwidth=2.5];',
             '    L4 [label="dynamic edge", shape=plaintext, fontcolor="blue"];',
             "  }"]

    shown = 0
    for bid, block in ir.blocks.items():
        if keep is not None and bid not in keep:
            continue
        shown += 1
        shape = _KIND_SHAPE.get(block.kind, "rect")
        attrs = [f"shape={shape}"]
        if bid in covered:
            attrs += ["style=filled", 'fillcolor="palegreen"']
            label = f"{bid}\\n hits={hits.get(bid, 0)}"
        else:
            attrs += ['color="gray"', 'fontcolor="gray"']
            label = bid
        if bid in session:
            # Overrides the gray border when uncovered: "state changes here,
            # never reached" must not look like an ordinary grey block.
            attrs += ['color="darkorange"', "penwidth=2.5"]
        attrs.append(f'label="{label}"')
        lines.append(f'  "{bid}" [{", ".join(attrs)}];')

    for edge in ir.edges:
        if edge.kind not in {"flow", "true", "false", "call", "return"}:
            continue
        if keep is not None and (edge.src not in keep or edge.dst not in keep):
            continue
        if only_learned and (edge.src, edge.dst) not in learned:
            continue
        attrs = []
        if (edge.src, edge.dst) in learned:
            attrs += ['color="blue"', "style=dashed", "penwidth=2.0",
                      'label="learned"']
        elif (edge.src, edge.dst) in frontier:
            attrs += ['color="red"', "penwidth=2.5", 'label="frontier"']
        elif edge.kind in ("call", "return"):
            # 属性必须整体放进一个方括号组(color 裸写在外面是 dot 语法错误)
            attrs += [f'label="{edge.kind}"', 'color="purple"']
        attr = f" [{', '.join(attrs)}]" if attrs else ""
        lines.append(f'  "{edge.src}" -> "{edge.dst}"{attr};')

    lines.append("}")
    out = Path(path)
    out.write_text("\n".join(lines), encoding="utf-8")
    if shown == 0:
        raise ValueError(
            "no blocks matched the filter; check the function name or line")
    return out
