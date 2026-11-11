"""Program IR for StateForge's structural feedback pipeline.

This module deliberately contains no web, state, or security concepts.  The
IR is the contract shared by static analysis, runtime trace mapping, and the
feedback engine.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Any


@dataclass
class BasicBlock:
    id: str
    file: str
    module: str
    function: str
    start_line: int
    end_line: int
    kind: str = "block"  # block/branch/terminal/entry/exit/loop_exit/try/callsite/returnsite
    statements: list[str] = field(default_factory=list)
    line_numbers: list[int] = field(default_factory=list)


@dataclass
class FlowEdge:
    src: str
    dst: str
    kind: str = "flow"  # flow, true, false, call, return
    condition: str | None = None
    callsite_line: int | None = None
    callee: str | None = None


@dataclass
class FunctionIR:
    name: str
    file: str
    module: str
    entry: str
    exit: str
    blocks: list[str] = field(default_factory=list)


@dataclass
class ProgramIR:
    blocks: dict[str, BasicBlock] = field(default_factory=dict)
    functions: dict[str, FunctionIR] = field(default_factory=dict)
    edges: list[FlowEdge] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    _by_src: dict[str, list[FlowEdge]] | None = field(
        default=None, repr=False, compare=False)
    _by_dst: dict[str, list[FlowEdge]] | None = field(
        default=None, repr=False, compare=False)
    _keys: set[tuple[str, str, str]] | None = field(
        default=None, repr=False, compare=False)
    _removed: set[tuple[str, str, str]] = field(
        default_factory=set, repr=False, compare=False)
    _pos: dict[tuple[str, str, str], int] = field(
        default_factory=dict, repr=False, compare=False)
    _indexed_len: int = field(default=-1, repr=False, compare=False)
    _dirty: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.edges:
            self._rebuild()

    def _rebuild(self) -> None:
        by_src: dict[str, list[FlowEdge]] = {}
        by_dst: dict[str, list[FlowEdge]] = {}
        keys: set[tuple[str, str, str]] = set()
        pos: dict[tuple[str, str, str], int] = {}
        for index, edge in enumerate(self.edges):
            key = (edge.src, edge.dst, edge.kind)
            if key in self._removed:
                continue
            by_src.setdefault(edge.src, []).append(edge)
            by_dst.setdefault(edge.dst, []).append(edge)
            keys.add(key)
            pos.setdefault(key, index)
        self._by_src, self._by_dst, self._keys = by_src, by_dst, keys
        self._pos = pos
        self._indexed_len = len(self.edges)
        self._dirty = bool(self._removed)

    def _index(self) -> tuple[dict[str, list[FlowEdge]],
                              dict[str, list[FlowEdge]],
                              set[tuple[str, str, str]]]:
        """Adjacency and identity sets, rebuilt when the list changes.

        Callers may append to :attr:`edges` directly (fixtures do), so the
        index is validated by length rather than assumed fresh.  Without it,
        ``add_edge``/``outgoing`` scan every edge and a large ICFG — one with
        a dependency slice — becomes quadratic.
        """
        if self._by_src is None or self._indexed_len != len(self.edges):
            self._rebuild()
        assert self._by_src is not None and self._by_dst is not None
        assert self._keys is not None
        return self._by_src, self._by_dst, self._keys

    def add_edge(self, edge: FlowEdge) -> None:
        by_src, by_dst, keys = self._index()
        key = (edge.src, edge.dst, edge.kind)
        if key in keys:
            return
        if key in self._removed:
            # A previously removed edge still sits in the uncompacted list.
            # Overwrite that slot: the caller may be re-adding the same pair
            # with a fresh condition, and keeping the stale object would make
            # the rewrite silently lose it.
            self._removed.discard(key)
            self.edges[self._pos[key]] = edge
        else:
            self._pos[key] = len(self.edges)
            self.edges.append(edge)
        by_src.setdefault(edge.src, []).append(edge)
        by_dst.setdefault(edge.dst, []).append(edge)
        keys.add(key)
        self._indexed_len = len(self.edges)

    def has_edge(self, src: str, dst: str, kind: str | None = None) -> bool:
        """Whether an exact edge identity is present (index-backed)."""
        by_src, _, _ = self._index()
        return any(e.dst == dst and (kind is None or e.kind == kind)
                   for e in by_src.get(src, ()))

    def remove_edge(self, src: str, dst: str, kind: str) -> None:
        """Drop an edge by identity (P1-2: chains replace direct block edges).

        The index is updated immediately but :attr:`edges` is compacted lazily:
        the flow rewrite removes and re-adds edges per block, and rewriting the
        whole list each time is what makes a large ICFG quadratic.
        """
        by_src, by_dst, keys = self._index()
        key = (src, dst, kind)
        if key not in keys:
            return
        keys.discard(key)
        self._removed.add(key)
        self._dirty = True
        for table, node in ((by_src, src), (by_dst, dst)):
            bucket = table.get(node)
            if bucket is not None:
                table[node] = [e for e in bucket
                               if (e.src, e.dst, e.kind) != key]

    def compact(self) -> None:
        """Apply pending removals to :attr:`edges`, if any.

        Reads of the edge list itself (serialization, counts) call this so the
        lazy index and the public list never disagree.
        """
        if not self._dirty:
            return
        self.edges = [e for e in self.edges
                      if (e.src, e.dst, e.kind) not in self._removed]
        self._pos = {key: index for index, e in enumerate(self.edges)
                     for key in [(e.src, e.dst, e.kind)]}
        self._removed.clear()
        self._dirty = False
        self._indexed_len = len(self.edges)

    def outgoing(self, node: str, kind: str | None = None) -> list[FlowEdge]:
        by_src, _, _ = self._index()
        return [e for e in by_src.get(node, ())
                if kind is None or e.kind == kind]

    def incoming(self, node: str, kind: str | None = None) -> list[FlowEdge]:
        _, by_dst, _ = self._index()
        return [e for e in by_dst.get(node, ())
                if kind is None or e.kind == kind]

    def to_dict(self) -> dict[str, Any]:
        self.compact()
        return {
            "blocks": {k: asdict(v) for k, v in self.blocks.items()},
            "functions": {k: asdict(v) for k, v in self.functions.items()},
            "edges": [asdict(e) for e in self.edges],
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProgramIR":
        return cls(
            blocks={k: BasicBlock(**v) for k, v in data.get("blocks", {}).items()},
            functions={k: FunctionIR(**v) for k, v in data.get("functions", {}).items()},
            edges=[FlowEdge(**v) for v in data.get("edges", [])],
            metadata=data.get("metadata", {}),
        )

    def to_json(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
                              encoding="utf-8")

    @classmethod
    def from_json(cls, path: str | Path) -> "ProgramIR":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
