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
    kind: str = "block"
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

    def add_edge(self, edge: FlowEdge) -> None:
        if not any(e.src == edge.src and e.dst == edge.dst and e.kind == edge.kind
                   for e in self.edges):
            self.edges.append(edge)

    def outgoing(self, node: str, kind: str | None = None) -> list[FlowEdge]:
        return [e for e in self.edges if e.src == node and (kind is None or e.kind == kind)]

    def incoming(self, node: str, kind: str | None = None) -> list[FlowEdge]:
        return [e for e in self.edges if e.dst == node and (kind is None or e.kind == kind)]

    def to_dict(self) -> dict[str, Any]:
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
