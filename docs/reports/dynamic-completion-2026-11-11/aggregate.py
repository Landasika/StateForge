"""Aggregate the dynamically-learned edges to the function level.

A block-level graph of a real target is thousands of nodes.  Every learned
edge is either *inside* one function (a `returnsite -> callsite` splice the
solver needed once the callee became known) or *between* two functions (a call
the static pass could not resolve).  Only the second kind answers "what did
fuzzing teach the call graph", so this view drops the first kind.

One trap: ``BasicBlock.function`` holds the *short* name, so grouping by it
merges ``routes.follow`` with ``models.User.follow``.  Real targets have many
such pairs (10 in microblog alone), and merging them both invents self-loops
and hides real cross-function edges.  The owning qname is recovered from
``ir.functions[...].blocks`` instead.

Usage::

    python aggregate.py microblog healthchecks CTFd
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from stateforge.analysis.static.icfg import build_icfg          # noqa: E402
from stateforge.fuzzing.dynamic_collect import collect_target    # noqa: E402
from stateforge.runtime.mapper import map_trace                  # noqa: E402

ROOTS = {p.name: p for p in (REPO / "external").iterdir() if p.is_dir()}
OUT = Path(__file__).resolve().parent / "graphs"


def short(qname: str) -> str:
    """Drop the leading package path; keep enough to stay unambiguous."""
    return ".".join(qname.split(".")[-2:])


def aggregate(label: str) -> dict:
    root = ROOTS[label].resolve()
    ir = build_icfg(root)
    result = collect_target(label, ir, root)
    if result.error:
        return {"label": label, "error": result.error}

    merged = build_icfg(root, dynamic=result.learned)
    static = {(e.src, e.dst) for e in ir.edges}
    new = {(e.src, e.dst) for e in merged.edges} - static

    owner: dict[str, str] = {}
    for qname, fn in merged.functions.items():
        for block_id in fn.blocks:
            owner[block_id] = qname

    covered = set(map_trace(result.events, merged).blocks)
    blocks_in = Counter(owner[b] for b in covered if b in owner)

    pairs: Counter[tuple[str, str]] = Counter()
    intra = 0
    orphan = 0
    for src, dst in new:
        a, b = owner.get(src), owner.get(dst)
        if a is None or b is None:
            orphan += 1
        elif a == b:
            intra += 1
        else:
            pairs[(a, b)] += 1

    degree: dict[str, int] = defaultdict(int)
    for (a, b), weight in pairs.items():
        degree[a] += weight
        degree[b] += weight

    # Callback-ish hubs: a node the static pass had no edge into at all but the
    # trace proved is reachable.  These are the framework hooks (`before_commit`)
    # and the recursion cycles, i.e. the classes of failure worth naming.
    static_in = {b for _, b in static}
    untyped_hubs = [q for q in degree if q not in static_in]

    return {
        "label": label, "error": None,
        "functions": len(merged.functions), "blocks": len(merged.blocks),
        "edges": len(merged.edges), "new_edges": len(new),
        "cross": sum(pairs.values()), "intra": intra, "orphan": orphan,
        "pairs": pairs, "degree": degree, "blocks_in": blocks_in,
        "untyped_hubs": untyped_hubs,
        "unresolved": len(merged.metadata.get("unresolved_calls", [])),
        "events": result.event_count, "learned": len(result.learned),
        "covered": len(covered),
    }


def write_dot(res: dict, path: Path) -> Path:
    pairs, degree, blocks_in = res["pairs"], res["degree"], res["blocks_in"]
    lines = [
        "digraph Learned {", "  rankdir=LR;",
        "  nodesep=0.35;", "  ranksep=0.9;",
        '  node [fontname="Helvetica", fontsize=10, shape=box,',
        '        style="rounded,filled"];',
        '  edge [fontname="Helvetica", fontsize=8, color="#1f6feb", arrowsize=0.7];',
        f'  labelloc="t"; label="{res["label"]}: {res["cross"]} learned '
        f'cross-function edges over {len(degree)} functions";',
    ]
    for q in sorted(degree, key=lambda x: -degree[x]):
        count = blocks_in.get(q, 0)
        fill = '"#d7f5dd"' if count else '"#eeeeee"'
        lines.append(f'  "{q}" [label="{short(q)}\\n{count} blocks", fillcolor={fill}];')
    for (a, b), weight in sorted(pairs.items()):
        lines.append(f'  "{a}" -> "{b}" [label="{weight}"];')
    lines.append("}")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> int:
    labels = sys.argv[1:]
    if not labels:
        print(__doc__)
        return 2
    OUT.mkdir(parents=True, exist_ok=True)
    for label in labels:
        try:
            res = aggregate(label)
        except Exception as exc:                       # noqa: BLE001
            print(f"[{label}] FAILED: {type(exc).__name__}: {exc}")
            continue
        if res["error"]:
            print(f"[{label}] harness error: {res['error']}")
            continue
        dot = write_dot(res, OUT / f"agg-{label}.dot")
        hubs = sorted(res["degree"].items(), key=lambda kv: -kv[1])[:5]
        print(f"[{label}] functions={res['functions']} blocks={res['blocks']} "
              f"events={res['events']} learned={res['learned']}")
        print(f"[{label}] new edges={res['new_edges']} "
              f"(cross={res['cross']} intra={res['intra']} orphan={res['orphan']}) "
              f"covered_blocks={res['covered']}")
        print(f"[{label}] top hubs: " +
              ", ".join(f"{short(q)}={v}" for q, v in hubs))
        if res["untyped_hubs"]:
            print(f"[{label}] no-static-inbound hubs: " +
                  ", ".join(short(q) for q in res["untyped_hubs"][:6]))
        print(f"[{label}] dot -> {dot}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
