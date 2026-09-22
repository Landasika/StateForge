"""Thin Atheris integration boundary.

StateForge supplies target scoring and corpus metadata; Atheris remains in
charge of mutation, execution, and native coverage accounting.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import hashlib
import json
import os
import sys

from stateforge.feedback.corpus import CorpusEntry, rank


class TraceJsonlWriter:
    """Incremental per-input trace sink.

    libFuzzer terminates the process with _Exit after -runs/-max_total_time,
    bypassing finally/atexit.  Every input is flushed immediately (and fsynced
    every ``fsync_every`` lines) so the artifact survives hard termination.
    """

    def __init__(self, path: str | Path, fsync_every: int = 100):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fsync_every = fsync_every
        self._count = 0
        self._fh = self.path.open("w", encoding="utf-8")

    def write_input(self, request_id: int, data: bytes,
                    exception: str | None, events: list) -> None:
        record = {
            "request_id": request_id,
            "data_sha256": hashlib.sha256(data).hexdigest(),
            "data_length": len(data),
            "exception": exception,
            "events": [asdict(event) for event in events],
        }
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._count += 1
        self._fh.flush()
        if self.fsync_every and self._count % self.fsync_every == 0:
            os.fsync(self._fh.fileno())

    def close(self) -> None:
        try:
            os.fsync(self._fh.fileno())
        except OSError:
            pass
        finally:
            self._fh.close()


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


def build_input_handler(adapter, session, writer: TraceJsonlWriter | None = None,
                        every_n: int = 1):
    """Per-input boundary: fresh request id, exception capture, trace line."""
    def handle(data: bytes) -> None:
        with session.request() as rid:
            start = len(session.events)
            error: str | None = None
            try:
                adapter.execute(data)
            except Exception as exc:      # fuzz inputs must not stop the engine
                error = f"{type(exc).__name__}: {exc}"
            if writer is not None and rid % every_n == 0:
                writer.write_input(rid, data, error, session.events[start:])
    return handle


def run_atheris(adapter, root: str | Path | None = None,
                argv: list[str] | None = None, trace_path: str | Path | None = None,
                corpus_dir: str | Path | None = None, every_n: int = 1,
                fsync_every: int = 100) -> None:
    """Drive Atheris over a TargetAdapter with structural tracing.

    Atheris owns mutation, scheduling, execution, and native coverage
    (via adapter.prepare instrumentation).  StateForge only records the
    ordered Python trace per input.  ``corpus_dir`` is handed to libFuzzer
    as its corpus directory so seeds persist across the process _Exit.
    """
    import atheris
    from stateforge.dynamic.tracer import TraceSession

    adapter.prepare(atheris)
    session = TraceSession(root)
    writer = TraceJsonlWriter(trace_path, fsync_every) if trace_path else None
    handler = build_input_handler(adapter, session, writer, every_n)
    fuzz_argv = list(sys.argv if argv is None else argv)
    if corpus_dir:
        Path(corpus_dir).mkdir(parents=True, exist_ok=True)
        fuzz_argv = [fuzz_argv[0], str(corpus_dir), *fuzz_argv[1:]]
    atheris.Setup(fuzz_argv, handler)
    try:
        with session:
            atheris.Fuzz()
    finally:
        if writer is not None:
            writer.close()      # best-effort; per-line flush already durable
