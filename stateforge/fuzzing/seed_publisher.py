"""Persist solver candidates using the shared corpus and lineage contract."""

from __future__ import annotations

from pathlib import Path

from stateforge.fuzzing.corpus_manager import CorpusManager


class SeedPublisher:
    def __init__(self, corpus_dir: str | Path, spool_name: str):
        self.corpus_dir = Path(corpus_dir)
        self.corpus_dir.mkdir(parents=True, exist_ok=True)
        self.spool = CorpusManager(self.corpus_dir.parent / spool_name)

    def save(self, data: bytes, *, origin: str, interesting_reason: str,
             nearest_frontier: str | None = None) -> str | None:
        """Return the content ID once, retaining each injector's lineage metadata."""
        cid = self.spool.save(
            data, epoch=0, new_blocks=0, new_edges=0,
            discovered_blocks=0, discovered_edges=0, origin=origin,
            interesting_reason=interesting_reason, nearest_frontier=nearest_frontier)
        if cid:
            (self.corpus_dir / cid).write_bytes(data)
        return cid
