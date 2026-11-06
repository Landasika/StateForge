"""Execution and evidence plumbing shared by structural feedback policies."""

from __future__ import annotations

from dataclasses import dataclass
import time

from stateforge.runtime.execution import execute_traced
from stateforge.runtime.execution_context import (
    ExecutionContext, LifecycleError, audit_lifecycle_failure,
)
from stateforge.runtime.mapper import MappedTrace, map_trace


@dataclass(frozen=True)
class CollectedExecution:
    mapped: MappedTrace
    state_novel: tuple
    execution_seconds: float
    feedback_started: float


class Collector:
    def __init__(self, ir, session, feedback, corpus, recorder, epoch,
                 on_save=None, execution=None, state_ledger=None,
                 state_novelty=False):
        self.ir, self.session, self.feedback = ir, session, feedback
        self.corpus, self.recorder, self.epoch = corpus, recorder, epoch
        self.on_save = on_save
        self.execution = execution if execution is not None else ExecutionContext()
        self.state_ledger = state_ledger
        self.state_novelty = state_novelty
        self.lifecycle_failures = 0
        self.last_mapped = None
        self.last_execution = None

    def _lifecycle_failed(self, exc: LifecycleError):
        self.lifecycle_failures += 1
        audit_lifecycle_failure(self.recorder.dir, {
            "group": self.recorder.group, "epoch": self.epoch,
            "lifecycle": self.execution.lifecycle.name,
            "isolation": self.execution.lifecycle.isolation,
            "error": str(exc)})

    def handle(self, target, data: bytes):
        self.last_mapped = None
        self.last_execution = None
        with self.session.untraced():
            try:
                run = execute_traced(target, data, self.session, self.execution)
            except LifecycleError as exc:
                self._lifecycle_failed(exc)
                return None
            self.last_execution = run
            novel = (() if self.state_ledger is None else
                     self.state_ledger.record(run.result.observations))
            started = time.monotonic()
            mapped = map_trace(run.events, self.ir)
            self.last_mapped = mapped
            return self._process(data, CollectedExecution(
                mapped, novel, run.execution_seconds, started))

    def _process(self, data: bytes, run: CollectedExecution):
        raise NotImplementedError
