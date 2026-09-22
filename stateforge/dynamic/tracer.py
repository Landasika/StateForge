"""Ordered Python execution trace collector."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sys
import threading
from typing import Any


@dataclass
class TraceEvent:
    request_id: int
    index: int
    file: str
    function: str
    line: int
    event_type: str = "line"


class TraceSession:
    def __init__(self, root: str | Path | None = None):
        self.root = str(Path(root).resolve()) if root else None
        self.events: list[TraceEvent] = []
        self._request_id = 0
        self._request_indices: dict[int, int] = {}
        self._previous = None
        self._lock = threading.Lock()

    def start_request(self) -> int:
        with self._lock:
            rid = self._request_id
            self._request_id += 1
            self._request_indices[rid] = 0
        return rid

    def _trace(self, frame, event, arg):
        if event in {"call", "line", "return"}:
            filename = str(Path(frame.f_code.co_filename).resolve())
            in_root = self.root is None or Path(filename).is_relative_to(self.root)
            if in_root:
                rid = getattr(self, "_active_request", 0)
                with self._lock:
                    index = self._request_indices.get(rid, 0)
                    self._request_indices[rid] = index + 1
                self.events.append(TraceEvent(rid, index, filename,
                                              frame.f_code.co_name, frame.f_lineno,
                                              event))
        return self._trace

    def __enter__(self):
        self._previous = sys.gettrace()
        self._active_request = self.start_request()
        sys.settrace(self._trace)
        return self

    def __exit__(self, exc_type, exc, tb):
        sys.settrace(self._previous)
        return False

    def request(self):
        session = self

        class _Request:
            def __enter__(self):
                session._active_request = session.start_request()
                return session._active_request

            def __exit__(self, exc_type, exc, tb):
                return False

        return _Request()

    def to_dict(self) -> list[dict[str, Any]]:
        return [asdict(e) for e in self.events]

    def to_json(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def from_json(cls, path: str | Path) -> list[TraceEvent]:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw = raw.get("events", raw.get("trace", []))
        return [TraceEvent(**item) for item in raw]
