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


@dataclass
class ProgramFacts:
    """Symbol table plus call sites for one analyzed program."""

    functions: dict[str, FunctionFact] = field(default_factory=dict)
    call_sites: list[CallSiteFact] = field(default_factory=list)

    def add_function(self, fact: FunctionFact) -> None:
        self.functions[fact.qname] = fact

    def add_call_site(self, site: CallSiteFact) -> None:
        self.call_sites.append(site)

    # -- indices used by the solver -------------------------------------
    def by_module_and_short(self, module: str, short: str) -> str | None:
        qname = f"{module}.{short}"
        return qname if qname in self.functions else None

    def by_short(self, short: str) -> list[str]:
        """Every declaration whose short (class-qualified) name matches."""
        return [q for q, f in self.functions.items() if f.short == short]

    def by_name(self, name: str) -> list[str]:
        """Every declaration whose bare name matches."""
        return [q for q, f in self.functions.items() if f.name == name]


def group_call_sites(sites: Iterable[CallSiteFact]) -> Mapping[str, list[CallSiteFact]]:
    """Group call sites by owning block, preserving frontend order."""
    grouped: dict[str, list[CallSiteFact]] = {}
    for site in sites:
        grouped.setdefault(site.host_block, []).append(site)
    return grouped
