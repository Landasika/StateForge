"""Language-neutral call-graph solver.

Consumes :mod:`stateforge.analysis.facts` and produces a
:class:`~stateforge.analysis.facts.Resolution` per call site.  The rules below
are deliberately *evidence ordered* rather than *language ordered*, so a new
frontend inherits all of them for free:

1. ``module_direct``  — the callee is declared in the caller's own module.
2. ``unique_short``   — exactly one declaration project-wide matches the short
   (class-qualified) name.
3. ``unique_alias``   — the qualified name resolves to a single declaration.
4. ``callgraph_hint`` — an externally supplied call graph (for example PyCG)
   disambiguates what name matching cannot.

Ambiguity is never broken arbitrarily: if more than one declaration matches and
no further evidence exists, the solver returns ``None`` and the call stays
unresolved.  Inventing an edge would silently corrupt every downstream
coverage and state metric, so refusing is the correct answer.

The solver does not know about source syntax, so it can answer for any language
whose frontend fills in :class:`~stateforge.analysis.facts.CallSiteFact`.
"""

from __future__ import annotations

from typing import Iterable, Mapping

from stateforge.analysis.facts import (
    PROV_CALLGRAPH_HINT,
    PROV_MODULE_DIRECT,
    PROV_UNIQUE_SHORT,
    CallSiteFact,
    ProgramFacts,
    Resolution,
)


class NameCallSolver:
    """Resolve call sites using declared names plus optional call-graph hints.

    ``known`` restricts what counts as a usable target: a name may resolve to a
    declaration that the current run did not build (for example it was filtered
    out), in which case the caller must treat the result as unresolved.  Pass
    the set of functions actually present in the IR; ``None`` means "trust the
    facts".
    """

    def __init__(self, facts: ProgramFacts, *,
                 known: Iterable[str] | None = None,
                 callgraph: Mapping[str, list[str]] | None = None) -> None:
        self.facts = facts
        self.known: set[str] | None = None if known is None else set(known)
        self.callgraph = callgraph

    # -- public API ------------------------------------------------------
    def resolve(self, site: CallSiteFact) -> Resolution | None:
        """Return the callee for *site*, or ``None`` when evidence is missing."""
        resolution = self._by_name(site)
        if resolution is None and site.callee_name and self.callgraph is not None:
            resolution = self._by_callgraph(site)
        return resolution

    def is_usable(self, resolution: Resolution | None) -> bool:
        """True when the answer names a function this run actually built."""
        if resolution is None:
            return False
        return self.known is None or resolution.callee in self.known

    # -- evidence rules --------------------------------------------------
    def _by_name(self, site: CallSiteFact) -> Resolution | None:
        short = site.callee_name
        if not short:
            return None

        caller = self.facts.functions.get(site.caller)
        if caller is not None:
            direct = self.facts.by_module_and_short(caller.module, short)
            if direct is not None:
                return Resolution(direct, PROV_MODULE_DIRECT)

        # Note: the previous implementation had a third step that read the
        # per-name alias list.  It was unreachable — the alias list is built
        # from exactly the same declarations as ``by_short``, so whenever it
        # held a single entry the unique-short rule had already answered, and
        # whenever it held several the answer was still "ambiguous".  Folding
        # it into the rule above keeps behaviour identical.
        candidates = self.facts.by_short(short)
        if len(candidates) == 1:
            return Resolution(candidates[0], PROV_UNIQUE_SHORT)
        return None

    def _by_callgraph(self, site: CallSiteFact) -> Resolution | None:
        """Ask a supplied call graph, matching qualified names by suffix.

        External tools prefix names with a repository path (for example
        ``vulnshop.app.a.handler``), so a suffix match bridges them to this
        project's names.  Ambiguous or absent hits stay unresolved.
        """
        caller, short = site.caller, site.callee_name
        if not short or self.callgraph is None:
            return None
        known = self.known if self.known is not None else set(self.facts.functions)
        callees: set[str] = set()
        for key, targets in self.callgraph.items():
            if key != caller and not key.endswith("." + caller):
                continue
            for target in targets:
                if target.rsplit(".", 1)[-1] != short:
                    continue
                if target in known:
                    callees.add(target)
                else:
                    for fn in known:
                        if target == fn or target.endswith("." + fn):
                            callees.add(fn)
        if len(callees) != 1:
            return None
        return Resolution(callees.pop(), PROV_CALLGRAPH_HINT)
