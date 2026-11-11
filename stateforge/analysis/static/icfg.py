"""Build a project-level interprocedural CFG (ICFG).

This module is the stable entry point.  The implementation now lives in three
places, split by how language dependent each part is:

* :mod:`stateforge.analysis.frontend.python` — Python syntax: file discovery,
  parsing, per-function CFG, expression-level call chains, and the
  ``CallSiteFact``/``FunctionFact`` facts it reports.
* :mod:`stateforge.analysis.frontend.pyslice` — which installed modules the
  application can reach, for the optional dependency slice (``deps``).
* :mod:`stateforge.analysis.solver.callgraph` — language-neutral resolution of
  those facts into call edges.

``build_icfg`` keeps its signature and metadata contract; ``_source_hash`` and
``_iter_owned_calls`` are re-exported because other modules and tests import
them from here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from stateforge.analysis.frontend.python import (
    _SKIP,
    _callee_name,
    _files,
    _iter_owned_calls,
    _module_name,
    _owned_expression_calls,
    _source_hash,
    _walk_with_class,
    build_python_icfg,
)
from stateforge.core.ir import ProgramIR

__all__ = ["build_icfg", "_source_hash", "_iter_owned_calls"]


def build_icfg(app_root: str | Path, callgraph: dict[str, list[str]] | None = None,
               include_all_functions: bool = True,
               dynamic: dict[tuple[str, int], str] | None = None,
               deps: bool | Iterable[Path | str] = False) -> ProgramIR:
    """Build the ICFG for the Python sources under *app_root*.

    ``dynamic`` maps ``(caller qname, line)`` to a callee observed at runtime;
    it is the strongest evidence available and is applied first (see
    :class:`~stateforge.analysis.solver.callgraph.NameCallSolver`).

    ``deps`` adds the installed modules the application can reach — the
    virtualenvs found under *app_root*, or the paths given explicitly.  It is
    off by default: coverage of the application's own code should not be
    diluted by library blocks, while route attribution needs the definitions
    that URLconf-resolved views live in.
    """
    return build_python_icfg(app_root, callgraph=callgraph,
                             include_all_functions=include_all_functions,
                             dynamic=dynamic, deps=deps)
