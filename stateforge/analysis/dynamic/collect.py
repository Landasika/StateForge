"""Drive a target's own harness under a trace, then hand the result back.

:mod:`stateforge.analysis.dynamic.callgraph` can turn a trace into call edges,
and :class:`~stateforge.analysis.solver.callgraph.NameCallSolver` can use them.
What was missing is the piece in between: something that actually *runs* a
target and keeps the events, without every caller re-inventing the driver.

The target's own harness is that driver.  Every application under
``stateforge/targets/apps20`` exposes ``smoke()`` -- a list of ``(path, status)``
for requests the target accepts -- against a module-level ``CLIENT`` that is
already authenticated and stateful.  So the recipe is uniform:

1. import the harness,
2. trace the process while ``smoke()`` issues its requests,
3. learn ``(caller qname, line) -> callee`` against a baseline IR.

The trace must be installed *after* the application is imported: module import
and migration run thousands of lines of framework bootstrapping that say
nothing about route behaviour, and tracing them both costs time and floods the
map with module-level frames that no route reaches.

Nothing here is framework specific.  ``smoke()`` is the harness convention, and
a caller holding any zero-argument callable can drive it directly;
:mod:`stateforge.fuzzing.dynamic_collect` is that caller for the app20 targets.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import importlib
import os
from pathlib import Path
import sys
from typing import Any, Callable, Iterable, Protocol

from stateforge.analysis.dynamic.callgraph import learn_call_edges
from stateforge.core.ir import ProgramIR

# The dynamic layer sits *below* ``stateforge.runtime`` in the layering, so it
# must not import the tracer.  Everything below works on any object shaped like
# a trace event -- which is what ``learn_call_edges`` already does -- and the
# part that actually drives a tracer lives in
# :mod:`stateforge.fuzzing.dynamic_collect`, which is allowed to reach up.
TraceEvent = Any

#: Harnesses live beside the target label: ``apps20/<label>/harness.py``.
HARNESS_PACKAGE = "stateforge.targets.apps20"


@dataclass
class Collection:
    """What one traced harness run produced."""

    label: str
    events: list[TraceEvent] = field(default_factory=list)
    requests: list[tuple[str, object]] = field(default_factory=list)
    learned: dict[tuple[str, int], str] = field(default_factory=dict)
    error: str | None = None

    @property
    def event_count(self) -> int:
        return len(self.events)


def harness_module(label: str):
    """Import ``apps20.<label>.harness``.

    The caller is responsible for running under the target's interpreter: a
    Django app only imports inside the virtualenv that has its dependencies.
    """
    return importlib.import_module(f"{HARNESS_PACKAGE}.{label}.harness")


def prepare_events(events: Iterable[TraceEvent], root: str | Path,
                   ir: ProgramIR) -> list[TraceEvent]:
    """Turn a raw trace into events the IR can be compared against.

    Two adjustments, in this order, and both are about *identity* rather than
    filtering -- an event is only usable when its ``(file, function)`` names a
    function the graph contains.

    1. Installed copies are rewritten onto the analysed source tree
       (:func:`path_translation`).  A project installed with ``pip install .``
       runs from ``site-packages`` while the graph comes from the source tree.
    2. Whatever is still outside *root*, or still inside the virtualenv, is
       dropped (:func:`project_events`).  That is framework code: it ran, but
       the graph has no nodes for it.
    """
    translation = path_translation(
        root, (f.file for f in ir.functions.values()))
    translated = translate_events(events, translation)
    return project_events(translated, root, app_exclusions(root))



def project_events(events: Iterable[TraceEvent], root: str | Path,
                   exclude: Iterable[str | Path] = ()) -> list[TraceEvent]:
    """Keep the events whose file is inside *root* and not under *exclude*.

    ``TraceSession`` filters by root prefix alone, which is not enough: an
    application with an in-tree virtualenv has its whole dependency tree
    *inside* the root.  ``external/microblog/.venv`` is under
    ``external/microblog``, so a plain prefix filter records some 1.4 million
    jinja2 compile events and the learned map drowns in frames no route reaches.

    *exclude* takes the dependency roots (an in-tree virtualenv, a
    ``site-packages``) exactly as :mod:`stateforge.analysis.frontend.python`
    excludes them from the scan.  Both layers agreeing on what "the
    application" means is what lets a trace event and an IR function be
    compared at all.
    """
    base = str(Path(root).resolve())
    prefix = base + os.sep
    banned = tuple(str(Path(p).resolve()) + os.sep for p in exclude)
    out = []
    for event in events:
        path = str(event.file)
        if not (path == base or path.startswith(prefix)):
            continue
        if any(path.startswith(b) for b in banned):
            continue
        out.append(event)
    return out


def app_exclusions(root: str | Path) -> list[Path]:
    """Directories inside *root* that hold installed code, not application code.

    A virtualenv in the tree is the common case, and it is discovered the way
    the dependency slice discovers one -- by ``pyvenv.cfg`` -- so ``venv`` and
    a custom name are handled alike.  A bare ``site-packages`` counts too, since
    that name is the import system's own convention.
    """
    from stateforge.analysis.frontend.pyslice import (
        _find_virtualenvs, _site_packages_of)

    out: list[Path] = []
    for venv in _find_virtualenvs(Path(root).resolve()):
        out.append(venv)
        out.extend(_site_packages_of(venv))
    return out


def path_translation(root: str | Path, files: Iterable[str | Path]) -> dict[str, str]:
    """Map installed copies onto the tree the IR was built from.

    A project installed with ``pip install .`` has its code *copied* into
    ``site-packages``, while the graph is built from the source tree.  The
    running program therefore reports ``site-packages/fava/core/number.py``
    for a function the IR knows as ``src/fava/core/number.py``.  Without a
    translation the two never meet and a project that ships itself as a package
    learns nothing at all -- fava, OctoPrint and Radicale all hit this.

    Translation is by suffix and verified by size: the installed file's path
    below ``site-packages`` is looked for under the analysis root, and accepted
    only when both files agree on length.  A mismatch means the installed copy
    is not the analysed source (``trac`` ships a different one), and leaving
    those events alone is what keeps a stale copy from rewriting line numbers
    the graph never had.
    """
    root_path = Path(root).resolve()
    known = {str(Path(f).resolve()) for f in files}
    by_suffix: dict[str, str] = {}
    for file in known:
        parts = Path(file).parts
        for depth in range(1, min(len(parts), 8)):
            by_suffix.setdefault(os.sep.join(parts[-depth:]), file)

    out: dict[str, str] = {}
    for site in app_exclusions(root_path):
        for installed in Path(site).rglob("*.py"):
            resolved = str(installed.resolve())
            if resolved in known:
                continue
            try:
                relative = installed.resolve().relative_to(Path(site).resolve())
            except ValueError:
                continue
            target = by_suffix.get(str(relative))
            if target is None:
                continue
            try:
                if installed.stat().st_size != Path(target).stat().st_size:
                    continue
            except OSError:
                continue
            out[resolved] = target
    return out


def translate_events(events: Iterable[TraceEvent],
                     translation: dict[str, str]) -> list[TraceEvent]:
    """Point installed-code events at the source files the IR contains."""
    if not translation:
        return list(events)
    out = []
    for event in events:
        target = translation.get(str(event.file))
        out.append(event if target is None else replace(event, file=target))
    return out




def uncovered_sites(ir: ProgramIR,
                    learned: dict[tuple[str, int], str]) -> list[dict]:
    """Unresolved call sites the trace did *not* settle.

    This is the honest residual: the sites still open after the dynamic pass.
    Reporting it keeps "we learned a lot" from standing in for "we closed the
    gap", and it is the list worth aiming the next run at.
    """
    out = []
    for row in ir.metadata.get("unresolved_calls", []):     # type: ignore[union-attr]
        caller, line = row.get("caller"), row.get("line")
        if not isinstance(caller, str) or not isinstance(line, int):
            continue
        if (caller, line) not in learned:
            out.append(row)
    return out


def interpreter_for(root: str | Path) -> str | None:
    """The virtualenv interpreter for an application tree, if it has one.

    A dependency's wheels are built for one interpreter; running the harness
    under a different one imports a different (or missing) Django.  Discovery
    goes through :mod:`stateforge.analysis.frontend.pyslice` so ``venv``/``env``
    work as well as ``.venv``.
    """
    from stateforge.analysis.frontend.pyslice import _find_virtualenvs

    for venv in _find_virtualenvs(Path(root).resolve()):
        for candidate in (venv / "bin" / "python", venv / "Scripts" / "python.exe"):
            if candidate.is_file():
                return str(candidate)
    return None


def ensure_importable(root: str | Path) -> None:
    """Put *root* first on ``sys.path`` so its package names resolve."""
    entry = str(Path(root).resolve())
    if entry not in sys.path:
        sys.path.insert(0, entry)
