"""Shortest-path structural distance on the ICFG."""

from __future__ import annotations

from collections import deque

from stateforge.core.ir import ProgramIR


def distance_to_targets(ir: ProgramIR, sources: set[str], targets: set[str]) -> dict[str, int]:
    """Return the shortest directed ICFG distance from any source to each target."""
    reverse: dict[str, list[str]] = {}
    for edge in ir.edges:
        reverse.setdefault(edge.dst, []).append(edge.src)
    dist: dict[str, int] = {}
    queue = deque((target, 0) for target in targets)
    while queue:
        node, d = queue.popleft()
        if node in dist and dist[node] <= d:
            continue
        dist[node] = d
        queue.extend((parent, d + 1) for parent in reverse.get(node, []))
    return {source: dist[source] for source in sources if source in dist}


def target_distances(ir: ProgramIR, targets: set[str]) -> dict[str, int]:
    return distance_to_targets(ir, set(ir.blocks), targets)
