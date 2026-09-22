"""Corpus metadata and deterministic structural ranking."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path

from stateforge.core.ir import ProgramIR
from stateforge.dynamic.mapper import MappedTrace
from stateforge.feedback.scorer import score_trace


@dataclass
class CorpusEntry:
    seed: str
    score: float
    blocks: int
    edges: int
    nearest_target: int | None = None


def evaluate_seed(ir: ProgramIR, seed: str, trace: MappedTrace,
                  targets: set[str]) -> CorpusEntry:
    distances = []
    if targets:
        from stateforge.feedback.distance import distance_to_targets
        distances = list(distance_to_targets(ir, set(trace.blocks), targets).values())
    return CorpusEntry(seed, score_trace(ir, trace, targets), len(trace.block_hits),
                       len(trace.edge_hits), min(distances) if distances else None)


def rank(entries: list[CorpusEntry]) -> list[CorpusEntry]:
    return sorted(entries, key=lambda e: (e.score, e.blocks, e.edges, e.seed), reverse=True)


def save(entries: list[CorpusEntry], path: str | Path) -> None:
    Path(path).write_text(json.dumps([asdict(e) for e in rank(entries)], indent=2),
                          encoding="utf-8")
