from pathlib import Path

from stateforge.runtime.io import merge_events
from stateforge.runtime.tracer import TraceEvent


def test_merge_events_reassigns_request_ids_and_indices(tmp_path: Path):
    for name, request in (("a.json", 0), ("b.json", 7)):
        (tmp_path / name).write_text(
            "[{\"request_id\": %d, \"index\": 99, \"file\": \"x.py\", "
            "\"function\": \"f\", \"line\": 1, \"event_type\": \"line\"}]" % request,
            encoding="utf-8",
        )
    events = merge_events([tmp_path / "a.json", tmp_path / "b.json"])
    assert [(event.request_id, event.index) for event in events] == [(0, 0), (1, 0)]
