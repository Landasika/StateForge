"""SS-07A: one physical request boundary for every target execution.

The ledger is deliberately small and dependency-free.  A target callable is
wrapped once; every actual invocation is one HTTP request.  Multi-step C10
inputs therefore produce one ledger ``sequence`` with several request events.
Counters are written incrementally, so a libFuzzer ``_Exit`` cannot erase the
tail of the accounting.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Callable


class BudgetExceeded(RuntimeError):
    """Raised *before* a target request that would exceed a hard budget."""


@dataclass
class ExecutionCosts:
    target_http_requests: int = 0
    sequence_attempts: int = 0
    atheris_inputs: int = 0
    bootstrap_http_requests: int = 0
    checkpoint_validation_http_requests: int = 0
    prefix_replay_http_requests: int = 0
    suffix_http_requests: int = 0
    observer_calls: int = 0
    reset_calls: int = 0
    scheduler_decisions: int = 0
    elapsed_wall_seconds: float = 0.0
    analysis_wall_seconds: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    def add(self, other: "ExecutionCosts | dict") -> None:
        values = other.to_dict() if isinstance(other, ExecutionCosts) else other
        for f in fields(self):
            setattr(self, f.name, getattr(self, f.name) + values.get(f.name, 0))


_PHASE_FIELD = {
    "bootstrap": "bootstrap_http_requests",
    "checkpoint": "checkpoint_validation_http_requests",
    "prefix_replay": "prefix_replay_http_requests",
    "suffix": "suffix_http_requests",
    "atheris": None,
    "replay": None,
}


class ExecutionCostLedger:
    """Per-run ledger with monotonic time and append-only JSONL events."""

    def __init__(self, *, out_dir: str | Path | None = None,
                 run_id: str = "", arm: str = "", seed: int = 0,
                 target_block: str = "", app: str = "",
                 request_limit: int | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.costs = ExecutionCosts()
        self.run_id = run_id
        self.arm = arm
        self.seed = seed
        self.target_block = target_block
        self.app = app
        self.request_limit = request_limit
        self._clock = clock
        self._started = clock()
        self._closed = False
        self._seq = 0
        self._sequence_id: str | None = None
        self._step = 0
        self._out = Path(out_dir) if out_dir is not None else None
        self._request_fp = None
        self._sequence_fp = None
        self._scheduler_fp = None
        if self._out is not None:
            self._out.mkdir(parents=True, exist_ok=True)
            self._request_fp = (self._out / "per_request.jsonl").open(
                "a", encoding="utf-8", buffering=1)
            self._sequence_fp = (self._out / "per_sequence.jsonl").open(
                "a", encoding="utf-8", buffering=1)
            self._scheduler_fp = (self._out / "scheduler.jsonl").open(
                "a", encoding="utf-8", buffering=1)
            self.snapshot()
        self._manifest()

    # ---------------------------------------------------------- events

    def _manifest(self) -> None:
        if self._out is None:
            return
        path = self._out / "ledger_manifest.json"
        if path.exists():
            return
        path.write_text(json.dumps({
            "run_id": self.run_id, "arm": self.arm, "seed": self.seed,
            "target_block": self.target_block, "app": self.app,
            "request_limit": self.request_limit,
            "boundary": "target-callable-invocation",
        }, ensure_ascii=False, indent=1), encoding="utf-8")

    def _write(self, fp, payload: dict) -> None:
        if fp is None:
            return
        try:
            fp.write(json.dumps(payload, ensure_ascii=False) + "\n")
            fp.flush()
        except OSError:
            # Accounting must not crash the fuzz target.  The in-memory
            # counters remain authoritative for normal exits.
            pass

    def _event(self, event: str, **fields) -> None:
        payload = {
            "event": event,
            "run_id": self.run_id,
            "arm": self.arm,
            "seed": self.seed,
            "app": self.app,
            "target_block": self.target_block,
        }
        payload.update(fields)
        self._write(self._request_fp, payload)

    # ---------------------------------------------------------- sequences

    def begin_sequence(self, kind: str = "atheris-input", **meta) -> str:
        if self._sequence_id is not None:
            self.end_sequence()
        self._seq += 1
        self._sequence_id = f"{self.run_id or 'run'}:seq:{self._seq}"
        self._step = 0
        self.costs.sequence_attempts += 1
        if kind == "atheris-input":
            self.costs.atheris_inputs += 1
        self._write(self._sequence_fp, {
            "event": "sequence_start", "sequence_id": self._sequence_id,
            "run_id": self.run_id, "arm": self.arm, "seed": self.seed,
            "kind": kind, **meta,
        })
        return self._sequence_id

    def end_sequence(self, status: str = "ok", **meta) -> None:
        if self._sequence_id is None:
            return
        self._write(self._sequence_fp, {
            "event": "sequence_end", "sequence_id": self._sequence_id,
            "run_id": self.run_id, "arm": self.arm, "seed": self.seed,
            "status": status, "steps": self._step, **meta,
        })
        self._sequence_id = None
        self._step = 0
        self.snapshot()

    # ---------------------------------------------------------- counters

    def _bump_phase(self, phase: str) -> None:
        field = _PHASE_FIELD.get(phase)
        if field:
            setattr(self.costs, field,
                    getattr(self.costs, field) + 1)

    def wrap_target(self, target: Callable[[bytes], Any], phase: str = "atheris"
                    ) -> Callable[[bytes], Any]:
        """Return a callable that counts each actual target invocation."""
        def wrapped(data: bytes):
            if (self.request_limit is not None
                    and self.costs.target_http_requests >= self.request_limit):
                self._event("budget_blocked", phase=phase,
                            limit=self.request_limit,
                            step=self._step + 1)
                raise BudgetExceeded(
                    f"target HTTP budget exhausted: {self.request_limit}")
            self.costs.target_http_requests += 1
            self._bump_phase(phase)
            self._step += 1
            started = self._clock()
            error = None
            try:
                return target(data)
            except BaseException as exc:       # count failures too
                error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                self._event(
                    "request", sequence_id=self._sequence_id,
                    step_index=self._step, phase=phase,
                    elapsed=round(self._clock() - started, 6),
                    error=error,
                    status="error" if error else "ok")
        return wrapped

    def wrap_reset(self, reset: Callable[[], Any]) -> Callable[[], Any]:
        def wrapped(*args, **kwargs):
            self.costs.reset_calls += 1
            started = self._clock()
            try:
                return reset(*args, **kwargs)
            finally:
                self._event("reset", elapsed=round(self._clock() - started, 6))
        return wrapped

    def wrap_observer(self, observe: Callable[[], Any]) -> Callable[[], Any]:
        def wrapped(*args, **kwargs):
            self.costs.observer_calls += 1
            started = self._clock()
            try:
                return observe(*args, **kwargs)
            finally:
                self._event("observer",
                            elapsed=round(self._clock() - started, 6))
        return wrapped

    def scheduler_decision(self, **fields) -> None:
        self.costs.scheduler_decisions += 1
        self._write(self._scheduler_fp, {
            "event": "scheduler_decision", "run_id": self.run_id,
            "arm": self.arm, "seed": self.seed, **fields,
        })

    def merge(self, other: "ExecutionCosts | dict") -> None:
        self.costs.add(other)

    def override_elapsed(self, seconds: float) -> None:
        self.costs.elapsed_wall_seconds = round(max(0.0, seconds), 6)

    # ---------------------------------------------------------- finish

    def costs_dict(self) -> dict:
        return self.costs.to_dict()

    def snapshot(self) -> None:
        if self._out is None:
            return
        costs = self.costs_dict()
        costs["elapsed_wall_seconds"] = round(
            max(costs["elapsed_wall_seconds"],
                self._clock() - self._started), 6)
        payload = {**costs, "run_id": self.run_id, "arm": self.arm,
                   "seed": self.seed, "target_block": self.target_block,
                   "app": self.app}
        tmp = self._out / "cost_snapshot.json.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(self._out / "cost_snapshot.json")

    def finish(self) -> dict:
        if not self._closed:
            self.costs.elapsed_wall_seconds = round(
                max(self.costs.elapsed_wall_seconds,
                    self._clock() - self._started), 6)
            self.snapshot()
            self._closed = True
            for fp in (self._request_fp, self._sequence_fp,
                       self._scheduler_fp):
                if fp is not None:
                    try:
                        fp.close()
                    except OSError:
                        pass
        return self.costs_dict()


def read_costs(out_dir: str | Path) -> dict:
    """Recompute costs from append-only events (crash-safe path)."""
    out = Path(out_dir)
    snap = out / "cost_snapshot.json"
    if snap.exists():
        try:
            data = json.loads(snap.read_text(encoding="utf-8"))
            return {k: data.get(k, 0) for k in ExecutionCosts().__dict__}
        except (OSError, json.JSONDecodeError):
            pass
    costs = ExecutionCosts()
    req = out / "per_request.jsonl"
    if req.exists():
        for line in req.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("event") == "request":
                costs.target_http_requests += 1
                phase = e.get("phase")
                field = _PHASE_FIELD.get(phase)
                if field:
                    setattr(costs, field, getattr(costs, field) + 1)
            elif e.get("event") == "reset":
                costs.reset_calls += 1
            elif e.get("event") == "observer":
                costs.observer_calls += 1
    seq = out / "per_sequence.jsonl"
    if seq.exists():
        for line in seq.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("event") == "sequence_start":
                costs.sequence_attempts += 1
                if e.get("kind") == "atheris-input":
                    costs.atheris_inputs += 1
    sched = out / "scheduler.jsonl"
    if sched.exists():
        costs.scheduler_decisions = sum(
            1 for line in sched.read_text(encoding="utf-8",
                                          errors="replace").splitlines()
            if line.strip())
    return costs.to_dict()
