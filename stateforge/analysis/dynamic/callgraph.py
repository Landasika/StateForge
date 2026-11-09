"""Learn call edges from a runtime trace.

Static analysis stops where the receiver's type is unknown; the interpreter does
not.  Every ``call`` event names both the site that made the call (the enclosing
frame and its current line) and the function that was entered, so a trace is a
direct observation of the call graph on the paths it covered.

The learned map is keyed by ``(caller qname, line)`` -- not by run, request or
input -- because a direct call site always targets the same function.  One
observation therefore upgrades later runs that reach the same site, which is
exactly what a fuzzer accumulates over time.

What this cannot do is cover code no input reached: dynamic evidence is bounded
by exploration, while static analysis is bounded by inference.  The two failures
are complementary, which is why feeding the observed edges back into the
solver (see :class:`~stateforge.analysis.solver.callgraph.NameCallSolver`)
recovers sites neither layer resolves alone.
"""

from __future__ import annotations

from typing import Iterable, Mapping

from stateforge.core.ir import ProgramIR
from stateforge.runtime.tracer import TraceEvent


def _caller_qname(ir: ProgramIR, file: str, function: str) -> str | None:
    """The single IR function a trace frame belongs to, else ``None``."""
    matches = [q for q, f in ir.functions.items()
               if f.file == file and f.name.rsplit(".", 1)[-1] == function]
    if len(matches) == 1:
        return matches[0]
    return None


def learn_call_edges(events: Iterable[TraceEvent],
                     ir: ProgramIR) -> dict[tuple[str, int], str]:
    """Map ``(caller qname, call line)`` to the callee the trace entered.

    A pair the trace shows pointing at two different callees (a genuinely
    polymorphic site, or two same-line calls) is dropped rather than guessed:
    the frozen line-only trace cannot say which call fired.
    """
    learned: dict[tuple[str, int], str] = {}
    conflicted: set[tuple[str, int]] = set()
    caller: tuple[str | None, int | None] = (None, None)

    for event in events:
        if event.event_type == "call":
            callee = _caller_qname(ir, event.file, event.function)
            caller_qname, line = caller
            if callee is not None and caller_qname is not None and line is not None:
                key = (caller_qname, line)
                previous = learned.get(key)
                if previous is not None and previous != callee:
                    conflicted.add(key)
                else:
                    learned[key] = callee
            caller = (callee, None)
            continue

        if event.event_type == "return":
            caller = (None, None)
            continue

        # A line event marks where execution currently is, i.e. the site the
        # next entered function will be called from.
        qname = _caller_qname(ir, event.file, event.function)
        if qname is not None:
            caller = (qname, event.line)

    for key in conflicted:
        learned.pop(key, None)
    return learned


def unresolved_sites(ir: ProgramIR) -> Mapping[tuple[str, int], str]:
    """Call sites the static pass could not resolve, keyed like the learned map."""
    out: dict[tuple[str, int], str] = {}
    for row in ir.metadata.get("unresolved_calls", []):        # type: ignore[union-attr]
        caller, line, callee = row.get("caller"), row.get("line"), row.get("callee")
        if isinstance(caller, str) and isinstance(line, int) and isinstance(callee, str):
            out[(caller, line)] = callee
    return out
