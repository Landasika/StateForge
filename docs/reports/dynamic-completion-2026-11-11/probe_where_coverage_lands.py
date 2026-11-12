"""Which files does each mutation mode actually cover?

The earlier A/B run reported byte mode covering 258 blocks and structured mode
123, which reads as "structured mutation is worse".  Before believing that, it
is worth asking *where* the covered blocks live.  If byte mode's advantage is
concentrated in template/library files, then it is not reaching more of the
application's logic -- it is re-rendering the same heavy page more often, and
raw block count is the wrong metric for the question being asked.

Usage::

    python probe_where_coverage_lands.py microblog 120
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))


def seeds_from_harness(target: str) -> list[bytes]:
    module = __import__(f"stateforge.targets.apps20.{target}.harness",
                        fromlist=["ROUTES"])
    out: list[bytes] = []
    for route in getattr(module, "ROUTES", []):
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


def run(target: str, executions: int, structured: bool):
    from collections import Counter as _Counter

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

    indeg: _Counter = _Counter(edge.dst for edge in ir.edges)
    candidates = [(indeg[bid], bid) for bid, block in ir.blocks.items()
                  if block.kind not in ("entry", "exit") and indeg[bid] > 0]
    candidates.sort(reverse=True)
    target_block = candidates[0][1]

    # Structured mode is opt-in, so the comparison must build both mutators
    # explicitly rather than rely on the campaign default.
    codec = getattr(adapter, "codec", None) if structured else None
    mutator = BasicMutator(random_seed=2026, codec=codec)
    config = CampaignConfig(max_executions=executions, max_seconds=None,
                            random_seed=2026, output_dir=None,
                            max_corpus_size=200)
    campaign = GuidedCampaign(ir, BasicBlockTarget(target_block), adapter,
                              mutator=mutator, config=config)
    campaign.run(seeds_from_harness(target))

    per_file: Counter = Counter()
    for bid in campaign.corpus.covered_blocks:
        block = ir.blocks.get(bid)
        if block is None:
            per_file["<not in IR>"] += 1
            continue
        path = str(block.file).replace(str(root), "").lstrip("/")
        # Collapse to the top two path components: "app/templates/x.html"
        # becomes "app/templates", which is enough to separate application
        # logic from vendored libraries.
        parts = path.split("/")
        key = "/".join(parts[:2]) if len(parts) > 2 else path
        per_file[key] += 1
    return campaign.corpus.covered_blocks, per_file


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else "microblog"
    executions = int(sys.argv[2]) if len(sys.argv) > 2 else 120
    for label, structured in (("byte", False), ("structured", True)):
        covered, per_file = run(target, executions, structured)
        print(f"\n=== {label}: {len(covered)} blocks ===")
        for name, count in per_file.most_common(12):
            print(f"  {count:5d}  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
