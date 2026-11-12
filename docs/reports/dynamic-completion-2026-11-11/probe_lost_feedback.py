"""Measure what the guided campaign throws away by not learning edges.

The loop already collects a full trace on every execution
(``campaign.py:218`` -> ``events``) but only ever calls ``map_trace``.  The
learning pass (``learn_call_edges``) is never invoked, so the answer to "which
callee did this unresolved site reach" is discarded on every single run.

This script replays that lost opportunity offline: it runs the same adapter
over the same seeds, feeds the resulting event stream to ``learn_call_edges``,
and reports how many *static unresolved sites* the trace would have closed.

Two counts matter, and they are different:

``learned``
    Every ``(caller, line) -> callee`` the trace proves.  Some of these the
    static pass already knew.

``would_close``
    The subset whose ``(caller, line)`` appears in ``ir.metadata
    ["unresolved_calls"]`` -- i.e. sites static analysis gave up on.  These are
    the ones feedback would actually repair.

Usage::

    python probe_lost_feedback.py microblog healthchecks
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

TARGETS = REPO / "stateforge" / "targets" / "apps20"
EXTERNAL = REPO / "external"


def probe(label: str) -> dict:
    """Run one target's harness and compare learned edges against unresolved."""
    from stateforge.analysis.dynamic.callgraph import (
        learn_call_edges,
        unresolved_sites,
    )
    from stateforge.analysis.static.icfg import build_icfg
    from stateforge.fuzzing.dynamic_collect import collect_target
    from stateforge.runtime.mapper import map_trace

    root = (EXTERNAL / label).resolve()
    ir = build_icfg(root)

    result = collect_target(label, ir, root)
    if result.error:
        return {"label": label, "error": result.error}

    # What the loop would learn if it called learn_call_edges on the trace it
    # already holds.
    learned = learn_call_edges(result.events, ir)

    # What static analysis gave up on, keyed the same way.
    open_sites = unresolved_sites(ir)

    closable = {k: v for k, v in learned.items() if k in open_sites}
    # Sanity: does the learned callee match what the static pass guessed it
    # would be?  A mismatch means the same line has several possible targets.
    agreeing = sum(1 for k, v in closable.items() if open_sites[k] == v)

    mapped = map_trace(result.events, ir)
    merged = build_icfg(root, dynamic=learned)
    static_edges = {(e.src, e.dst) for e in ir.edges}
    new_edges = {(e.src, e.dst) for e in merged.edges} - static_edges

    return {
        "label": label,
        "error": None,
        "functions": len(ir.functions),
        "blocks": len(ir.blocks),
        "edges": len(ir.edges),
        "unresolved": len(open_sites),
        "events": len(result.events),
        "mapped_blocks": len(set(mapped.blocks)),
        "unresolved_events": len(mapped.unresolved),
        "learned": len(learned),
        "would_close": len(closable),
        "agreeing": agreeing,
        "new_edges": len(new_edges),
    }


def main() -> int:
    labels = sys.argv[1:]
    if not labels:
        print(__doc__)
        return 2

    rows = []
    for label in labels:
        try:
            row = probe(label)
        except Exception as exc:                       # noqa: BLE001
            print(f"[{label}] FAILED {type(exc).__name__}: {exc}")
            continue
        if row["error"]:
            print(f"[{label}] harness error: {row['error']}")
            continue
        rows.append(row)
        rate = (row["would_close"] / row["unresolved"] * 100
                if row["unresolved"] else 0.0)
        agree = (row["agreeing"] / row["would_close"] * 100
                 if row["would_close"] else 0.0)
        print(f"[{label}]")
        print(f"  static        funcs={row['functions']} blocks={row['blocks']} "
              f"edges={row['edges']} unresolved_sites={row['unresolved']}")
        print(f"  trace         events={row['events']} "
              f"mapped_blocks={row['mapped_blocks']} "
              f"unresolved_events={row['unresolved_events']}")
        print(f"  learned       edges={row['learned']} "
              f"would_close={row['would_close']} ({rate:.1f}% of unresolved)")
        print(f"  callee agrees with static guess: {row['agreeing']}/{row['would_close']} "
              f"({agree:.0f}%)")
        print(f"  new graph edges if fed back: {row['new_edges']}")

    if len(rows) > 1:
        print("\n=== totals ===")
        print(f"  unresolved_sites  {sum(r['unresolved'] for r in rows)}")
        print(f"  would_close       {sum(r['would_close'] for r in rows)} "
              f"({sum(r['would_close'] for r in rows) / sum(r['unresolved'] for r in rows) * 100:.1f}%)")
        print(f"  new_edges         {sum(r['new_edges'] for r in rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
