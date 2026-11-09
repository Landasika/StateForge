"""Persist learned call edges so the loop compounds.

One trace teaches the graph a little; the fuzzing loop runs many traces.  Keeping
the observations on disk is what turns a single shallow run into a graph that
improves monotonically, which is the whole point of "dynamic execution repairs
the static graph".

Two rules keep the cache honest:

* **Conflicts are dropped, not resolved.**  If two traces show the same
  ``(caller, line)`` reaching different callees, the site is polymorphic or the
  line-only trace cannot tell two same-line calls apart.  Keeping either one
  would inject a false edge, which is worse than no edge.
* **Stale entries expire with the source.**  Entries are keyed by caller qname
  and line, so a source edit can silently move a line onto a different call.
  The cache therefore carries a source hash and is discarded when it changes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping


@dataclass
class DynamicEdgeCache:
    """Accumulated ``(caller qname, line) -> callee qname`` observations."""

    edges: dict[tuple[str, int], str] = field(default_factory=dict)
    conflicts: set[tuple[str, int]] = field(default_factory=set)
    source_hash: str | None = None

    # -- construction ----------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> "DynamicEdgeCache":
        p = Path(path)
        if not p.is_file():
            return cls()
        data = json.loads(p.read_text(encoding="utf-8"))
        edges = {(str(k["caller"]), int(k["line"])): str(k["callee"])
                 for k in data.get("edges", [])}
        conflicts = {(str(c["caller"]), int(c["line"]))
                     for c in data.get("conflicts", [])}
        return cls(edges=edges, conflicts=conflicts,
                   source_hash=data.get("source_hash"))

    def save(self, path: str | Path) -> None:
        rows = [{"caller": caller, "line": line, "callee": callee}
                for (caller, line), callee in sorted(self.edges.items())]
        bad = [{"caller": caller, "line": line} for caller, line in sorted(self.conflicts)]
        Path(path).write_text(
            json.dumps({"source_hash": self.source_hash, "edges": rows,
                        "conflicts": bad}, indent=2, ensure_ascii=False),
            encoding="utf-8")

    # -- accumulation ----------------------------------------------------
    def update(self, learned: Mapping[tuple[str, int], str],
               source_hash: str | None = None) -> int:
        """Merge *learned* in; returns how many entries became newly usable.

        A site that already has a different target is marked conflicted and
        removed: refusing the edge is the only non-guessing choice.
        """
        if source_hash is not None and self.source_hash not in (None, source_hash):
            # The sources changed under the cache; start over rather than carry
            # line numbers that may now point somewhere else.
            self.edges.clear()
            self.conflicts.clear()
        if source_hash is not None:
            self.source_hash = source_hash

        gained = 0
        for key, callee in learned.items():
            if key in self.conflicts:
                continue
            existing = self.edges.get(key)
            if existing is None:
                self.edges[key] = callee
                gained += 1
            elif existing != callee:
                self.edges.pop(key, None)
                self.conflicts.add(key)
        return gained

    def as_mapping(self) -> dict[tuple[str, int], str]:
        return dict(self.edges)


def learn_into_cache(cache: DynamicEdgeCache,
                     learned: Iterable[tuple[tuple[str, int], str]],
                     source_hash: str | None = None) -> int:
    return cache.update(dict(learned), source_hash=source_hash)
