"""Shortest-path structural distance on the ICFG."""

from __future__ import annotations

from collections import deque

from stateforge.core.ir import ProgramIR


def reverse_graph(ir: ProgramIR) -> dict[str, list[str]]:
    reverse: dict[str, list[str]] = {}
    for edge in ir.edges:
        reverse.setdefault(edge.dst, []).append(edge.src)
    return reverse


def reverse_distances(reverse: dict[str, list[str]], anchors: dict[str, str]
                      ) -> tuple[dict[str, int], dict[str, str]]:
    """Multi-source BFS; anchor insertion order resolves equal-distance owners."""
    distances = dict.fromkeys(anchors, 0)
    nearest = dict(anchors)
    queue = deque(anchors)
    while queue:
        node = queue.popleft()
        for parent in reverse.get(node, []):
            if parent not in distances:
                distances[parent] = distances[node] + 1
                nearest[parent] = nearest[node]
                queue.append(parent)
    return distances, nearest


def distance_to_targets(ir: ProgramIR, sources: set[str], targets: set[str]) -> dict[str, int]:
    """Return the shortest directed ICFG distance from sources to a target set."""
    distances, _ = reverse_distances(reverse_graph(ir), {t: t for t in targets})
    return {source: distances[source] for source in sources if source in distances}


def target_distances(ir: ProgramIR, targets: set[str]) -> dict[str, int]:
    return distance_to_targets(ir, set(ir.blocks), targets)
