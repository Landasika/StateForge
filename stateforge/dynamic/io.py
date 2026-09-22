"""Trace artifact loading and deterministic multi-run merging."""

from __future__ import annotations

import json
from pathlib import Path

from stateforge.dynamic.tracer import TraceEvent


def load_events(path: str | Path) -> list[TraceEvent]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("events", raw.get("trace", []))
    return [TraceEvent(**item) for item in raw]


def merge_events(paths: list[str | Path]) -> list[TraceEvent]:
    """Merge trace files while assigning disjoint request IDs.

    A seed/request boundary is preserved as a request ID.  Event indices are
    renumbered within each input so artifacts created by independent workers
    can be combined without relying on their local counters.
    """
    merged: list[TraceEvent] = []
    request_offset = 0
    for path in paths:
        events = load_events(path)
        request_ids = sorted({event.request_id for event in events})
        remap = {rid: request_offset + index for index, rid in enumerate(request_ids)}
        next_index: dict[int, int] = {}
        for event in events:
            request_id = remap.get(event.request_id, request_offset)
            index = next_index.get(request_id, 0)
            next_index[request_id] = index + 1
            merged.append(TraceEvent(request_id, index, event.file, event.function,
                                     event.line, event.event_type))
        request_offset += len(request_ids)
    return merged
