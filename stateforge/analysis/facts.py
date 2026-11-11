"""Language-neutral program facts and call-graph resolution records.

This module is the contract between a *frontend* (one per source language,
which understands syntax) and the *solver* (a single implementation that
combines evidence into call edges).

Design note
-----------
A frontend must not decide call targets.  It only reports what it can observe
syntactically:

* which functions exist, and in which lexical scope (``FunctionFact``);
* which call expressions occur, with what callee name and what kind of
  receiver the syntax implies (``CallSiteFact``).

The solver then answers "which function does this call go to?", and every
answer carries the :class:`Resolution.provenance` that justified it.  Keeping
provenance is what allows metrics to stay comparable across languages: Python
can only offer name evidence where Java offers declared types, but the origin
of each edge remains visible.

Nothing here imports a language parser, so adding a language means adding a
frontend only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping


@dataclass(frozen=True)
class FunctionFact:
    """A function or method as declared in source.

    ``class_path`` is the chain of lexically enclosing class names, so a method
    keeps its qualified identity (``module.Class.method``) and same-named
    methods in different classes are never merged.
    """

    qname: str
    module: str
    name: str
    class_path: tuple[str, ...] = ()
    file: str = ""
    start_line: int | None = None
    end_line: int | None = None
    params: tuple[str, ...] = ()
    return_type: str | None = None

    @property
    def short(self) -> str:
        return ".".join(self.class_path + (self.name,)) if self.class_path else self.name


#: Receiver shapes a frontend may report.  Kept deliberately small and
#: syntax-oriented: these are *not* types, just what the call expression looks
#: like.  ``NONE`` = bare name, ``SELF`` = ``self.x()``/``cls.x()``,
#: ``MODULE`` = ``mod.x()``, ``EXPR`` = ``expr.x()`` (receiver computed).
NONE = "none"
SELF = "self"
MODULE = "module"
EXPR = "expr"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class CallSiteFact:
    """One call expression reported by a frontend."""

    caller: str                      # qname of the enclosing function
    callee_name: str | None          # syntactic name, e.g. "check" or "manifest"
    line: int
    column: int = 0
    host_block: str = ""             # block whose expression owns this call
    receiver: str = UNKNOWN
    receiver_symbol: str | None = None

    @property
    def sort_key(self) -> tuple[int, int]:
        """Evaluation order within a block (position, not merely line)."""
        return (self.line, self.column)


@dataclass(frozen=True)
class Resolution:
    """A solver answer: the callee, and why."""

    callee: str
    provenance: str
    confidence: float = 1.0


#: Provenance vocabulary.  Frontends never invent new values; the solver
#: assigns exactly one of these so downstream metrics can group by evidence.
PROV_MODULE_DIRECT = "module_direct"      # same module declares this name
PROV_UNIQUE_SHORT = "unique_short"        # exactly one declaration project-wide
PROV_CALLGRAPH_HINT = "callgraph_hint"    # supplied external call graph
PROV_SELF_METHOD = "self_method"          # method of the caller's own class
PROV_BASE_METHOD = "base_method"          # method inherited from a local base
PROV_ANNOTATION = "annotation"            # receiver type from an annotation
PROV_IMPORT = "import"                    # qualified through an import table
PROV_DYNAMIC = "dynamic"                  # observed at runtime for this site


@dataclass(frozen=True)
class ImportFact:
    """``local`` is bound to ``target`` (a module path) inside ``scope``.

    For ``import a.b as x`` the local is ``x`` and the target is ``a.b``; for
    ``from a.b import c`` the local is ``c`` and the target is ``a.b``.  The
    solver turns ``mod.f``/``f`` into a qualified candidate with this instead
    of guessing by name, which is the only sound way to read ``mod.f()``.
    """

    scope: str
    local: str
    target: str


@dataclass(frozen=True)
class ClassFact:
    """A class declaration, as far as call resolution cares about it.

    ``bases`` keeps base names *as written*; a frontend does not resolve them,
    and the solver only follows ones that map unambiguously onto classes
    declared in the analysed program.
    """

    qname: str
    module: str
    name: str
    bases: tuple[str, ...] = ()
    methods: tuple[str, ...] = ()

    def has_method(self, method: str) -> bool:
        return method in self.methods


@dataclass(frozen=True)
class TypeFact:
    """``symbol`` is known to hold a value of ``type_name`` within ``scope``.

    Deliberately weak: this is evidence, not a type system.  A frontend records
    what it can establish cheaply (an annotation, later an assignment) and the
    solver uses it to pick a method target.
    """

    scope: str          # qname of the function where the symbol is bound
    symbol: str         # variable/parameter name
    type_name: str      # simple type name as written, e.g. "User"
    source: str = "annotation"


#: Where a :class:`TypeFact` came from, so metrics can separate declared from
#: inferred types.
TYPE_ANNOTATION = "annotation"
TYPE_ASSIGNMENT = "assignment"


@dataclass
class ProgramFacts:
    """Symbol table plus call sites for one analyzed program.

    The lookups below are answered from an index built on first use and
    invalidated on every addition, so queries are dict hits instead of scans.
    That matters once a dependency slice puts tens of thousands of
    declarations here: the solver asks per call site, and a linear scan turns
    the whole build quadratic.
    """

    functions: dict[str, FunctionFact] = field(default_factory=dict)
    classes: dict[str, ClassFact] = field(default_factory=dict)
    types: list[TypeFact] = field(default_factory=list)
    imports: list[ImportFact] = field(default_factory=list)
    call_sites: list[CallSiteFact] = field(default_factory=list)
    _index: dict | None = field(default=None, repr=False, compare=False)

    def add_function(self, fact: FunctionFact) -> None:
        self.functions[fact.qname] = fact
        self._index = None

    def add_class(self, fact: ClassFact) -> None:
        self.classes[fact.qname] = fact
        self._index = None

    def add_type_fact(self, fact: TypeFact) -> None:
        self.types.append(fact)
        self._index = None

    def add_import(self, fact: ImportFact) -> None:
        self.imports.append(fact)
        self._index = None

    def add_call_site(self, site: CallSiteFact) -> None:
        self.call_sites.append(site)

    def _indices(self) -> dict:
        if self._index is None:
            by_short: dict[str, list[str]] = {}
            by_name: dict[str, list[str]] = {}
            for qname, fact in self.functions.items():
                by_short.setdefault(fact.short, []).append(qname)
                by_name.setdefault(fact.name, []).append(qname)
            classes: dict[str, list[str]] = {}
            for qname, cls in self.classes.items():
                classes.setdefault(cls.name, []).append(qname)
            imports: dict[tuple[str, str], set[str]] = {}
            for fact in self.imports:
                imports.setdefault((fact.scope, fact.local), set()).add(fact.target)
            types: dict[tuple[str, str], list[TypeFact]] = {}
            for fact in self.types:
                types.setdefault((fact.scope, fact.symbol), []).append(fact)
            self._index = {"short": by_short, "name": by_name,
                           "classes": classes, "imports": imports,
                           "types": types}
        return self._index

    # -- indices used by the solver -------------------------------------
    def by_module_and_short(self, module: str, short: str) -> str | None:
        qname = f"{module}.{short}"
        return qname if qname in self.functions else None

    def by_short(self, short: str) -> list[str]:
        """Every declaration whose short (class-qualified) name matches."""
        return self._indices()["short"].get(short, [])

    def by_name(self, name: str) -> list[str]:
        """Every declaration whose bare name matches."""
        return self._indices()["name"].get(name, [])

    def class_by_simple_name(self, name: str) -> list[str]:
        """Project classes whose simple name matches *name*."""
        return self._indices()["classes"].get(name, [])

    def method_of(self, class_qname: str, method: str) -> str | None:
        """The method declared directly by *class_qname*, if it exists."""
        cls = self.classes.get(class_qname)
        if cls is None or not cls.has_method(method):
            return None
        qname = f"{class_qname}.{method}"
        return qname if qname in self.functions else None

    def import_target(self, scope: str, local: str) -> str | None:
        """Module that ``local`` refers to in ``scope``, when unambiguous.

        Two imports of the same local name from different modules (a genuine
        redefinition) yield ``None``: picking one would be a guess.
        """
        targets = self._indices()["imports"].get((scope, local), set())
        return next(iter(targets)) if len(targets) == 1 else None

    def types_of(self, scope: str, symbol: str) -> list[TypeFact]:
        return self._indices()["types"].get((scope, symbol), [])


def group_call_sites(sites: Iterable[CallSiteFact]) -> Mapping[str, list[CallSiteFact]]:
    """Group call sites by owning block, preserving frontend order."""
    grouped: dict[str, list[CallSiteFact]] = {}
    for site in sites:
        grouped.setdefault(site.host_block, []).append(site)
    return grouped
