"""Language-neutral call-graph solver.

Consumes :mod:`stateforge.analysis.facts` and produces a
:class:`~stateforge.analysis.facts.Resolution` per call site.  The rules below
are *evidence ordered*, not language ordered, so a new frontend inherits all of
them for free:

1. ``self_method``/``base_method`` — the receiver is the caller's own class.
2. ``annotation``  — the receiver carries a known local type.
3. ``module_direct`` — a bare call naming something the caller's module declares.
4. ``import``      — the callee (or the receiver) is qualified by an import:
   ``mod.f()`` means "``f`` in whatever ``mod`` denotes".
5. ``unique_short``  — exactly one declaration project-wide matches the short
   (class-qualified) name.
6. ``callgraph_hint`` — an externally supplied call graph (for example PyCG).

Rules 3 and 5 apply **only to bare-name calls**.  A call written ``x.f()`` on a
local variable has no evidence about ``x`` unless a type or an import supplies
it, and matching by name alone connects ``row.get()`` to whatever module-level
``def get`` the project happens to have.  That is a false edge, and a false edge
is worse than a missing one: it silently credits coverage to a function that was
never called.  So receiver-bearing calls without receiver evidence stay
unresolved.

Ambiguity is never broken arbitrarily either: if more than one declaration
matches and no further evidence exists, the solver returns ``None`` and the call
stays unresolved.  Inventing an edge would silently corrupt every downstream
coverage and state metric, so refusing is the correct answer.

The solver does not know about source syntax, so it can answer for any language
whose frontend fills in :class:`~stateforge.analysis.facts.CallSiteFact`.
"""

from __future__ import annotations

from typing import Iterable, Mapping

from stateforge.analysis.facts import (
    NONE,
    PROV_ANNOTATION,
    PROV_BASE_METHOD,
    PROV_CALLGRAPH_HINT,
    PROV_IMPORT,
    PROV_MODULE_DIRECT,
    PROV_SELF_METHOD,
    PROV_UNIQUE_SHORT,
    SELF,
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
        """Return the callee for *site*, or ``None`` when evidence is missing.

        A receiver-bearing call is answered only by receiver evidence:
        ``self.x()`` targets a method of the caller's class, ``mod.x()`` targets
        what ``mod`` was imported as, and ``var.x()`` needs a known type.
        """
        evidence = (self._by_self_method,
                    self._by_annotated_receiver,
                    self._by_local_name,
                    self._by_import,
                    self._by_unique_short)
        for rule in evidence:
            resolution = rule(site)
            if resolution is not None:
                return resolution
        if site.callee_name and self.callgraph is not None:
            return self._by_callgraph(site)
        return None

    def is_usable(self, resolution: Resolution | None) -> bool:
        """True when the answer names a function this run actually built."""
        if resolution is None:
            return False
        return self.known is None or resolution.callee in self.known

    # -- evidence rules --------------------------------------------------
    def _by_self_method(self, site: CallSiteFact) -> Resolution | None:
        """``self.x()`` / ``cls.x()`` -> the caller's own class, then its bases."""
        if site.receiver != SELF or not site.callee_name:
            return None
        caller = self.facts.functions.get(site.caller)
        if caller is None or not caller.class_path:
            return None
        class_qname = ".".join([caller.module, *caller.class_path])
        return self._method_in_hierarchy(class_qname, site.callee_name,
                                         PROV_SELF_METHOD)

    def _by_annotated_receiver(self, site: CallSiteFact) -> Resolution | None:
        """``u.x()`` where ``u`` carries a local type annotation for ``x``.

        Only fires when the annotation names a class declared in this project
        exactly once, so an annotation on a library type simply yields no
        evidence instead of a speculative edge.
        """
        name = site.callee_name
        if not name or not site.receiver_symbol:
            return None
        if site.receiver == SELF:
            return None                       # handled by the stronger rule
        for fact in self.facts.types_of(site.caller, site.receiver_symbol):
            classes = self.facts.class_by_simple_name(fact.type_name)
            if len(classes) != 1:
                continue
            found = self._method_in_hierarchy(classes[0], name, PROV_ANNOTATION)
            if found is not None:
                return found
        return None

    def _method_in_hierarchy(self, class_qname: str, method: str,
                             provenance: str, _seen: set[str] | None = None
                             ) -> Resolution | None:
        """Find *method* on the class or, failing that, on a local base class."""
        seen = _seen if _seen is not None else set()
        if class_qname in seen:
            return None                       # cycle guard for bad hierarchies
        seen.add(class_qname)

        direct = self.facts.method_of(class_qname, method)
        if direct is not None:
            return Resolution(direct, provenance)

        cls = self.facts.classes.get(class_qname)
        if cls is None:
            return None
        inherited = PROV_BASE_METHOD if provenance == PROV_SELF_METHOD else provenance
        for base in cls.bases:
            bases = self.facts.class_by_simple_name(base.rsplit(".", 1)[-1])
            if len(bases) != 1:
                continue                      # unknown or ambiguous base
            found = self._method_in_hierarchy(bases[0], method, inherited, seen)
            if found is not None:
                return found
        return None

    def _by_local_name(self, site: CallSiteFact) -> Resolution | None:
        """A bare call naming a function the caller's own module declares."""
        if site.receiver != NONE or not site.callee_name:
            return None
        caller = self.facts.functions.get(site.caller)
        if caller is None:
            return None
        return self._by_module_and_short(caller.module, site.callee_name,
                                         PROV_MODULE_DIRECT)

    def _by_import(self, site: CallSiteFact) -> Resolution | None:
        """Qualify the callee through the caller module's import table.

        ``from a.b import c`` followed by ``c()`` names ``a.b.c``; the same
        import followed by ``c.d()`` names ``a.b.c.d`` (``c`` may be a module).
        ``import a.b as x`` followed by ``x.f()`` names ``a.b.f``.  Turning a
        guess into a lookup is the only sound reason to resolve ``mod.f()``.
        """
        short = site.callee_name
        if not short:
            return None
        caller = self.facts.functions.get(site.caller)
        if caller is None:
            return None
        scope = caller.module

        if site.receiver == NONE:
            if self._by_module_and_short(scope, short, PROV_MODULE_DIRECT):
                return None                   # a local declaration wins
            base = self.facts.import_target(scope, short)
            if base is None:
                return None
            candidates = [base]
        elif site.receiver_symbol:
            base = self.facts.import_target(scope, site.receiver_symbol)
            if base is None:
                return None
            candidates = [f"{base}.{short}"]
        else:
            return None

        for candidate in candidates:
            for qname in self._package_variants(candidate):
                if qname in self.facts.functions:
                    return Resolution(qname, PROV_IMPORT)
        return None

    def _by_module_and_short(self, module: str, short: str,
                             provenance: str) -> Resolution | None:
        for qname in self._package_variants(f"{module}.{short}"):
            if qname in self.facts.functions:
                return Resolution(qname, provenance)
        return None

    def _package_variants(self, qname: str) -> list[str]:
        """``a.b`` plus ``a.__init__.b``: a package's own names live in ``__init__``."""
        if "." not in qname:
            return [qname]
        head, _, tail = qname.rpartition(".")
        return [qname, f"{head}.__init__.{tail}"]

    def _by_unique_short(self, site: CallSiteFact) -> Resolution | None:
        short = site.callee_name
        if not short or site.receiver != NONE:
            # No receiver evidence: refuse rather than glue ``x.f()`` onto a
            # module-level ``def f`` that merely shares the name.
            return None
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
