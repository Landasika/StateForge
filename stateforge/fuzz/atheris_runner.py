"""Thin Atheris integration boundary.

StateForge supplies target scoring and corpus metadata; Atheris remains in
charge of mutation, execution, and native coverage accounting.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import sys

from stateforge.feedback.corpus import CorpusEntry, rank


@dataclass
class SeedScore:
    seed: str
    score: float
    blocks: int
    edges: int


def rank_seed_scores(scores: list[SeedScore]) -> list[SeedScore]:
    return sorted(scores, key=lambda item: (item.score, item.blocks, item.edges), reverse=True)


def rank_corpus(entries: list[CorpusEntry]) -> list[CorpusEntry]:
    """Rank metadata for the next Atheris corpus selection round."""
    return rank(entries)


def save_feedback(scores: list[SeedScore], path: str | Path) -> None:
    Path(path).write_text(json.dumps([s.__dict__ for s in rank_seed_scores(scores)],
                                     indent=2), encoding="utf-8")


def run_atheris(target, root: str | Path | None = None,
                argv: list[str] | None = None,
                trace_path: str | Path | None = None) -> None:
    """Run Atheris with a StateForge tracing harness.

    This adapter intentionally delegates mutation, scheduling, and execution
    to Atheris.  It only accumulates the ordered Python events needed for a
    later ``cli feedback`` pass.  Atheris is imported lazily so structural
    analysis and unit tests do not require the optional runtime dependency.
    """
    import atheris
    from stateforge.fuzz.harness import run_one

    all_events = []

    def test_one_input(data: bytes) -> None:
        result = run_one(data, target, root=root)
        all_events.extend(result.trace)

    setup_argv = argv if argv is not None else sys.argv
    atheris.Setup(setup_argv, test_one_input)
    try:
        atheris.Fuzz()
    finally:
        if trace_path is not None:
            Path(trace_path).write_text(
                json.dumps([event.__dict__ for event in all_events], indent=2),
                encoding="utf-8",
            )
