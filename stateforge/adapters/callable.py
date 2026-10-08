"""Adapter for plain ``target(data: bytes)`` callables."""

from __future__ import annotations

from typing import Callable

from stateforge.fuzzing.instrumentation import Instrumenter


class CallableAdapter:
    def __init__(self, target: Callable[[bytes], object], instrument: bool = True):
        self.target = target
        self.instrument = instrument
        self._instrumenter = Instrumenter(registry=set())

    def prepare(self, atheris) -> None:
        if self.instrument:
            self._instrumenter.instrument_callable(self.target, atheris)

    def execute(self, data: bytes) -> object:
        return self.target(data)
