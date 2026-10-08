"""Minimal Atheris harness boundary.

The harness owns only input dispatch and trace collection.  Mutation and native
coverage remain Atheris responsibilities.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import asdict
import json
from pathlib import Path
from typing import Callable

from stateforge.runtime.tracer import TraceSession


@dataclass
class HarnessResult:
    data: bytes
    exception: str | None
    trace: list

    def to_dict(self) -> dict:
        return {
            "data": self.data.hex(),
            "exception": self.exception,
            "events": [asdict(event) for event in self.trace],
        }

    def save_trace(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")


def run_one(data: bytes, target: Callable[[bytes], object], root: str | None = None) -> HarnessResult:
    session = TraceSession(root)
    error = None
    with session:
        try:
            target(data)
        except Exception as exc:  # fuzz inputs must not stop the engine
            error = f"{type(exc).__name__}: {exc}"
    return HarnessResult(data, error, session.events)
