"""End-to-end check: does structured mutation reach more of the target?

The offline count in the mutator tests only proves the *output* parses.  That
is necessary but not sufficient: a mutation can be valid JSON and still be a
request the app rejects in the same way as every other one.  This script runs
the real campaign twice -- byte mode versus the adapter's own codec -- and
compares the coverage each one reaches for the same execution budget.

Both runs share the seed corpus, the random seed and the budget, so any
difference is attributable to the mutator and not to sampling noise.

Usage::

    python probe_structured_mutation.py [target] [executions]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))


def seeds_from_harness(target: str) -> list[bytes]:
    """Harness routes rendered as the adapter's own request envelope."""
    module = __import__(f"stateforge.targets.apps20.{target}.harness",
                        fromlist=["ROUTES"])
    routes = getattr(module, "ROUTES", [])
    out: list[bytes] = []
    for route in routes:
        method, path = route[0], route[1]
        form = route[2] if len(route) > 2 else None
        headers = route[3] if len(route) > 3 else None
        envelope: dict = {"method": method, "path": path}
        if form is not None:
            envelope["body"] = form
        if headers is not None:
            envelope["headers"] = dict(headers)
        out.append(json.dumps(envelope).encode())
    return out


def run(target: str, executions: int, structured: bool) -> dict:
    from stateforge.analysis.static.icfg import build_icfg
    from stateforge.fuzzing.guidance.campaign import (
        CampaignConfig,
        GuidedCampaign,
    )
    from stateforge.fuzzing.guidance.mutator import BasicMutator
    from stateforge.fuzzing.guidance.target import BasicBlockTarget

    module = __import__(f"stateforge.targets.apps20.{target}.fuzz_target",
                        fromlist=["_ADAPTER"])
    adapter = getattr(module, "_ADAPTER")

    root = (REPO / "external" / target).resolve()
    ir = build_icfg(root)
    # A block in the middle of the app: reaching it needs a real request, not
    # the adapter's default GET /.
    target_block = _pick_target_block(ir)

    # Both runs pass a mutator explicitly: structured mode is opt-in, so the
    # comparison must not depend on campaign's default.
    codec = getattr(adapter, "codec", None) if structured else None
    mutator = BasicMutator(random_seed=2026, codec=codec)
    config = CampaignConfig(
        max_executions=executions,
        max_seconds=None,
        random_seed=2026,
        output_dir=None,
        max_corpus_size=200,
    )
    campaign = GuidedCampaign(
        ir, BasicBlockTarget(target_block), adapter,
        mutator=mutator, config=config)
    seeds = seeds_from_harness(target)
    result = campaign.run(seeds)
    return {
        "executions": result.executions,
        "covered": len(campaign.corpus.covered_blocks),
        "edges": len(campaign.corpus.covered_edges),
        "trace_events": campaign.total_trace_events,
        "mapping_failures": campaign.mapping_failures,
        "target_reached": result.target_reached,
        "best_distance": campaign.best_distance,
    }


def _pick_target_block(ir) -> str:
    """The block with the most static predecessors among non-entry blocks.

    A well-connected block is reachable only through real request handling,
    so it discriminates "adapter ran the app" from "adapter returned 404".
    """
    from collections import Counter

    indeg: Counter = Counter(edge.dst for edge in ir.edges)
    candidates = [
        (indeg[bid], bid) for bid, block in ir.blocks.items()
        if block.kind not in ("entry", "exit") and indeg[bid] > 0
    ]
    candidates.sort(reverse=True)
    return candidates[0][1]


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else "microblog"
    executions = int(sys.argv[2]) if len(sys.argv) > 2 else 400

    rows = {}
    for label, structured in (("byte", False), ("structured", True)):
        print(f"[{target}] running {label} ...", flush=True)
        rows[label] = run(target, executions, structured)

    print(f"\n=== {target}, budget {executions} executions ===")
    for label, row in rows.items():
        print(f"  {label:11s} covered={row['covered']:5d} "
              f"edges={row['edges']:5d} events={row['trace_events']:6d} "
              f"mapfail={row['mapping_failures']:5d} "
              f"best_dist={row['best_distance']}")
    base = rows["byte"]["covered"]
    new = rows["structured"]["covered"]
    if base:
        print(f"\n  coverage change: {new - base:+d} "
              f"({(new - base) / base * 100:+.1f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
