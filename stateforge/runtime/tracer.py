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
        # 定向灰盒(M1):请求号改为线程局部,使并发请求各自归属、互不串号;
        # session 级 __enter__ 仍写 _active_request,裸 `with session:` 的旧
        # 口径(全部事件归属 rid=0)保持不变。
        self._local = threading.local()
        self._active_request = 0

    def _current_request(self) -> int:
        rid = getattr(self._local, "request_id", None)
        if rid is not None:
            return rid
        return getattr(self, "_active_request", 0)

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
            rid = self._current_request()
            with self._lock:
                index = self._request_indices.get(rid, 0)
                self._request_indices[rid] = index + 1
            self.events.append(TraceEvent(rid, index, resolved,
                                          frame.f_code.co_name, frame.f_lineno,
                                          event))
        return self._trace

    def __enter__(self):
        stack = getattr(self._local, "session_stack", None)
        if stack is None:
            stack = []
            self._local.session_stack = stack
        # 嵌套 session 各自还原外层 settrace 与请求号,不再依赖单槽 _previous。
        stack.append((sys.gettrace(), getattr(self._local, "request_id", None)))
        self._previous = sys.gettrace()
        self._active_request = self.start_request()
        self._local.request_id = self._active_request
        sys.settrace(self._trace)
        return self

    def __exit__(self, exc_type, exc, tb):
        stack = getattr(self._local, "session_stack", None) or []
        previous_trace, previous_request = (stack.pop() if stack
                                            else (self._previous, None))
        sys.settrace(previous_trace)
        self._local.request_id = previous_request
        self._active_request = (previous_request
                                if previous_request is not None else 0)
        return False

    def request(self):
        session = self

        class _Request:
            def __enter__(self):
                self._previous = getattr(session._local, "request_id", None)
                rid = session.start_request()
                session._local.request_id = rid
                session._active_request = rid
                return rid

            def __exit__(self, exc_type, exc, tb):
                # 还原外层请求号:嵌套请求结束后,后续帧仍归属外层请求。
                session._local.request_id = self._previous
                session._active_request = (self._previous
                                           if self._previous is not None else 0)
                return False

        return _Request()

    @contextlib.contextmanager
    def trace_execution(self):
        """只装/卸 settrace,不分配请求号。

        定向灰盒 campaign 的用法:``with session.trace_execution():`` 包裹
        多次 ``with session.request():``,每次输入各自一个请求窗口,事件
        按请求号排空(drain_request_events),不混入相邻输入。
        """
        previous = sys.gettrace()
        sys.settrace(self._trace)
        try:
            yield
        finally:
            sys.settrace(previous)

    def drain_request_events(self, request_id: int) -> list[TraceEvent]:
        """取出并移除某次请求的事件(按请求隔离,供逐输入反馈)。

        session 全局 ``events`` 口径不变——只移除被 drain 的那一号请求,
        其余请求的事件原样保留,故旧调用方读 ``session.events`` 不受影响。
        """
        with self._lock:
            drained = [event for event in self.events
                       if event.request_id == request_id]
            if drained:
                self.events = [event for event in self.events
                               if event.request_id != request_id]
            self._request_indices.pop(request_id, None)
        return drained

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
