"""Drive a target's own harness under a trace, then hand the result back.

:mod:`stateforge.analysis.dynamic.callgraph` turns a trace into call edges and
:mod:`stateforge.analysis.dynamic.collect` decides which of those events
describe the application.  What was missing is the piece in between: something
that actually *runs* a target and keeps the events.

That piece lives here rather than in ``analysis`` because it starts a tracer.
The dynamic layer sits below ``stateforge.runtime`` in the architecture, so it
may only see events through a structural type; installing a ``TraceSession`` is
a caller's job.  This module is that caller.

The target's own harness is the driver.  Every application under
``stateforge/targets/apps20`` exposes ``smoke()`` -- a list of ``(path, status)``
for requests it accepts -- against a module-level ``CLIENT`` that is already
authenticated and stateful, so the recipe is uniform across all of them.

The trace is installed *after* the application is imported: module import and
migration run thousands of lines of framework bootstrapping that say nothing
about route behaviour, and tracing them both costs time and floods the map with
module-level frames no route reaches.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
from typing import Callable

from stateforge.analysis.dynamic.callgraph import learn_call_edges
from stateforge.analysis.dynamic.collect import (
    Collection, harness_module, prepare_events)
from stateforge.core.ir import ProgramIR
from stateforge.runtime.tracer import TraceSession


def collect_events(drive: Callable[[], object], root: str | Path,
                   label: str = "") -> Collection:
    """Run *drive* under a trace rooted at *root* and keep the events.

    *root* bounds what gets recorded.  Pointing it at the application tree --
    not the virtualenv -- keeps installed framework code out of the trace, so
    the learned edges describe the application's own call sites.  A broken
    target must not kill the run: its failure is reported and the events
    gathered so far are still returned.
    """
    result = Collection(label=label)
    session = TraceSession(root)
    try:
        with session:
            produced = drive()
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        produced = None
    result.events = session.events
    if isinstance(produced, (list, tuple)):
        result.requests = [row for row in produced if isinstance(row, tuple)]
    return result


@contextmanager
def _restored_cwd():
    """Put the working directory back after a harness run.

    Harnesses ``chdir`` into their application so relative config and fixture
    paths resolve -- correct for them, disruptive for the caller.  Without this
    every relative path the caller holds afterwards, including the analysis
    root, silently points somewhere else.
    """
    before = os.getcwd()
    try:
        yield
    finally:
        try:
            os.chdir(before)
        except OSError:                             # the old directory can be gone
            pass


def collect_target(label: str, ir: ProgramIR, root: str | Path,
                   drive: Callable[[], object] | None = None) -> Collection:
    """Trace a target's ``smoke()`` and learn edges against *ir*.

    The learned map is keyed by ``(caller qname, line)`` exactly as
    :meth:`~stateforge.analysis.solver.callgraph.NameCallSolver` expects, so it
    can be passed straight to ``build_icfg(..., dynamic=learned)``.

    Two corrections happen before anything is learned, both because a trace
    only informs the graph if its events describe functions the graph contains:
    *root* is resolved up front (importing a harness may change the working
    directory), and :func:`prepare_events` reconciles installed copies with the
    analysed tree before dropping the framework code.
    """
    root = Path(root).resolve()
    if drive is None:
        with _restored_cwd():
            drive = harness_module(label).smoke
    with _restored_cwd():
        result = collect_events(drive, root, label)
    result.events = prepare_events(result.events, root, ir)
    if result.events:
        result.learned = learn_call_edges(result.events, ir)
    return result
