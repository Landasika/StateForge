"""Ordered Python execution trace collector."""

from __future__ import annotations

import contextlib
from dataclasses import asdict, dataclass
import json
import os
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


_SELF_FILE = __file__      # tracer 不得 trace 自身:root=None 时会自锁死锁(见 T P1-3)


class TraceSession:
    def __init__(self, root: str | Path | None = None):
        self.root = str(Path(root).resolve()) if root else None
        self.events: list[TraceEvent] = []
        self._request_id = 0
        self._request_indices: dict[int, int] = {}
        self._previous = None
        self._lock = threading.Lock()
        # filename -> (在 root 内?, 已解析绝对路径);热路径逐事件只做一次 dict 查找
        self._file_cache: dict[str, tuple[bool, str]] = {}

    def start_request(self) -> int:
        with self._lock:
            rid = self._request_id
            self._request_id += 1
            self._request_indices[rid] = 0
        return rid

    def _trace(self, frame, event, arg):
        if frame.f_code.co_filename == _SELF_FILE:
            return None                     # 自身帧不追踪(防 root=None 自锁死锁)
        if event not in {"call", "line", "return", "exception"}:
            return self._trace
        filename = frame.f_code.co_filename
        cached = self._file_cache.get(filename)
        if cached is None:
            resolved = str(Path(filename).resolve())
            if self.root is None:
                cached = (True, resolved)
            else:
                inside = resolved == self.root or resolved.startswith(self.root + os.sep)
                cached = (inside, resolved)
            self._file_cache[filename] = cached
        inside, resolved = cached
        if inside:
            rid = getattr(self, "_active_request", 0)
            with self._lock:
                index = self._request_indices.get(rid, 0)
                self._request_indices[rid] = index + 1
            self.events.append(TraceEvent(rid, index, resolved,
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

    @contextlib.contextmanager
    def untraced(self):
        """P0-B:执行面挂起——窗口外的帧不入 events。

        语义澄清(阶段 1 审计确认):``request()`` 只切换事件**归属**
        (request_id),不停止收集——settrace 在整个 session 存续期生效,
        窗口外(如观测器/基线还原)在 root 内执行的帧照样进 events、
        照样被 map_trace 计入覆盖。观测/还原属于反馈机制自身,不是被测
        执行,必须经本上下文显式挂起,防"反馈膨胀覆盖"。
        """
        prev = sys.gettrace()
        sys.settrace(None)
        try:
            yield
        finally:
            sys.settrace(prev)

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
