"""Target adapter contract."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class TargetAdapter(Protocol):
    def prepare(self, atheris) -> None:
        """One-time setup before atheris.Fuzz(): instrumentation etc."""

    def execute(self, data: bytes) -> object:
        """Run one fuzz input against the target."""
