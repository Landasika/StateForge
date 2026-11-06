"""Single input execution with lifecycle, trace ownership and bounded consumption."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable

from stateforge.runtime.execution_context import ExecutionContext, ExecutionResult
from stateforge.runtime.tracer import TraceEvent, TraceSession


@dataclass(frozen=True)
class TracedExecution:
    result: ExecutionResult
    events: tuple[TraceEvent, ...]
    execution_seconds: float
    response: Any = None
    error: Exception | None = None


def execute_traced(target: Callable[[bytes], Any], data: bytes,
                   session: TraceSession, context: ExecutionContext) -> TracedExecution:
    """Execute once; observe before restore; always release this request's trace.

    Ordinary target exceptions are recorded. Lifecycle errors propagate so the
    caller can reject all feedback from a failed isolation boundary. Observer,
    restoration and feedback frames are never traced by this service.
    """
    with session.untraced():
        with session.request() as request_id:
            try:
                response, error = None, None
                started = time.monotonic()
                with session.trace_execution():
                    context.begin_run()
                    try:
                        response = target(data)
                    except Exception as exc:
                        error = exc
                    finally:
                        elapsed = time.monotonic() - started
                        # Restoration must also run for process-control errors.
                        with session.untraced():
                            result = context.finish_run(data)
            finally:
                events = tuple(session.drain_request_events(request_id))
        return TracedExecution(result, events, elapsed, response, error)
