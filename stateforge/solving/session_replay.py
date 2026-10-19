"""SS-04: verified Session checkpoint + cold-start prefix replay.

A checkpoint is created only from a guided result that has both TargetReached
and a runtime-verified Session witness.  Replay always starts from a reset
(fresh client), executes the verified prefix, re-observes the Cookie Jar, and
re-evaluates the stored condition group.  A suffix, when supplied, runs after
the verified prefix and is judged independently; a suffix run without the
prefix is the isolation control.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from stateforge.core.ir import ProgramIR
from stateforge.fuzzing.entry_driver import encode_sequence_seed
from stateforge.runtime.mapper import map_trace
from stateforge.runtime.tracer import TraceSession
from stateforge.state.runtime_observer import Evidence, StateObservation
from stateforge.state.session_index import SessionCondition
from stateforge.state.session_observation import (
    SESSION_STATUS_ABSENT,
    SESSION_STATUS_DECODED,
    SessionGroupVerdict,
    SessionSnapshotView,
    evaluate_session_conditions,
    session_observations,
    snapshot_view,
    VERIFIED_TRUE,
)

REPLAY_VERIFIED = "REPLAY_VERIFIED"
REPLAY_TARGET_ONLY = "TARGET_ONLY"
REPLAY_FAILED = "REPLAY_FAILED"
REPLAY_UNKNOWN = "REPLAY_UNKNOWN"


@dataclass
class SessionReplayCheckpoint:
    checkpoint_id: str
    target_block: str
    source_file: str
    steps: list[dict]                     # direct C10 request dicts
    condition_groups: list[list[dict]]    # serialized SessionCondition groups
    session_values: dict = field(default_factory=dict)
    writer_blocks: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    prefix_len: int = 0
    protocol_version: str = "session-checkpoint-v1"
    created_from: str = "session-guided"

    def __post_init__(self) -> None:
        if self.prefix_len <= 0:
            self.prefix_len = len(self.steps)
        if not (0 < self.prefix_len <= len(self.steps)):
            raise ValueError("prefix_len 必须在 (0, len(steps)] 内")

    def prefix_steps(self) -> list[dict]:
        return [dict(s) for s in self.steps[:self.prefix_len]]

    def suffix_steps(self) -> list[dict]:
        return [dict(s) for s in self.steps[self.prefix_len:]]

    def sequence_seed(self, steps: list[dict] | None = None) -> bytes:
        return encode_sequence_seed(
            [dict(s) for s in (self.steps if steps is None else steps)])

    def prefix_seed(self) -> bytes:
        return self.sequence_seed(self.prefix_steps())

    def to_dict(self) -> dict:
        return {
            "checkpoint_id": self.checkpoint_id,
            "protocol_version": self.protocol_version,
            "created_from": self.created_from,
            "target_block": self.target_block,
            "source_file": self.source_file,
            "steps": self.steps,
            "prefix_len": self.prefix_len,
            "condition_groups": self.condition_groups,
            "session_values": self.session_values,
            "writer_blocks": self.writer_blocks,
            "evidence": self.evidence,
        }

    def save(self, directory: str | Path) -> Path:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.checkpoint_id}.json"
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False,
                                   indent=1), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "SessionReplayCheckpoint":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(**d)

    def condition_objects(self) -> list[list[SessionCondition]]:
        return [[SessionCondition.from_dict(c) for c in group]
                for group in self.condition_groups]


@dataclass(frozen=True)
class SessionReplayResult:
    checkpoint_id: str
    target_block: str
    status: str
    replay_success: bool
    target_covered: bool
    conditions_verified: bool
    values: dict | None
    group_verdicts: tuple[SessionGroupVerdict, ...] = ()
    prefix_len: int = 0
    suffix_len: int = 0
    suffix_target_covered: bool | None = None
    suffix_conditions_verified: bool | None = None
    trace_covered: tuple[str, ...] = ()
    started_from_reset: bool = True
    reason: str = ""
    observations: tuple[StateObservation, ...] = ()

    def to_dict(self) -> dict:
        return {
            "checkpoint_id": self.checkpoint_id,
            "target_block": self.target_block,
            "status": self.status,
            "replay_success": self.replay_success,
            "target_covered": self.target_covered,
            "conditions_verified": self.conditions_verified,
            "values": self.values,
            "group_verdicts": [g.to_dict() for g in self.group_verdicts],
            "prefix_len": self.prefix_len,
            "suffix_len": self.suffix_len,
            "suffix_target_covered": self.suffix_target_covered,
            "suffix_conditions_verified": self.suffix_conditions_verified,
            "trace_covered": list(self.trace_covered),
            "started_from_reset": self.started_from_reset,
            "reason": self.reason,
            "observations": [o.to_dict() for o in self.observations],
        }


def checkpoint_from_guided_result(result, source_file: str,
                                  *, prefix_len: int | None = None
                                  ) -> SessionReplayCheckpoint | None:
    """Only a VERIFIED guided result may become a checkpoint."""
    if not getattr(result, "checkpoint_eligible", False):
        return None
    if not result.session_witness or not result.session_witness.get("values"):
        return None
    steps = [dict(s) for s in result.steps]
    if not steps:
        return None
    groups = [result.session_witness.get("conditions", [])]
    if not groups or not groups[0]:
        return None
    prefix = len(steps) if prefix_len is None else int(prefix_len)
    payload = json.dumps({"target": result.target_block, "steps": steps,
                          "conditions": groups},
                         sort_keys=True, ensure_ascii=False).encode("utf-8")
    values = dict(result.session_witness["values"])
    view = SessionSnapshotView(SESSION_STATUS_DECODED, values,
                               client_id="client-0",
                               observation_point="after_verified_prefix")
    evidence = [o.to_dict() for o in session_observations(view)]
    return SessionReplayCheckpoint(
        checkpoint_id=f"sc-{hashlib.sha256(payload).hexdigest()[:12]}",
        target_block=result.target_block, source_file=str(source_file),
        steps=steps, condition_groups=groups, session_values=values,
        writer_blocks=list(result.writer_blocks), evidence=evidence,
        prefix_len=prefix)


def _view_from_probe(probe) -> SessionSnapshotView:
    if probe is None:
        return SessionSnapshotView("unknown")
    raw = probe()
    if isinstance(raw, SessionSnapshotView):
        return raw
    return snapshot_view(raw, client_id="client-0",
                         observation_point="after_prefix")


def _execute_prefix(target, steps: list[dict], ir: ProgramIR,
                    trace_root, reset_hook: Callable[[], None] | None,
                    probe) -> tuple[set[str], SessionSnapshotView, tuple]:
    if reset_hook is not None:
        reset_hook()
    trace = TraceSession(root=trace_root)
    view = SessionSnapshotView("unknown")
    try:
        with trace:
            for step in steps:
                target(json.dumps(step, ensure_ascii=False,
                                  sort_keys=True).encode("utf-8"))
            view = _view_from_probe(probe)
    finally:
        if reset_hook is not None:
            reset_hook()
    mapped = map_trace(trace.events, ir)
    return set(mapped.block_hits), view, tuple(mapped.block_hits)


def replay_session_checkpoint(ckpt: SessionReplayCheckpoint, target,
                              ir: ProgramIR, *, reset_hook=None,
                              session_probe=None, trace_root=None,
                              suffix_steps: list[dict] | None = None,
                              ) -> SessionReplayResult:
    """Fresh-client prefix replay; optional suffix runs after the prefix."""
    if trace_root is None:
        blk = ir.blocks.get(ckpt.target_block)
        trace_root = Path(blk.file).parent if blk else None
    groups = [tuple(g) for g in ckpt.condition_objects()]
    prefix = ckpt.prefix_steps()
    suffix = [dict(s) for s in (suffix_steps if suffix_steps is not None
                                else ckpt.suffix_steps())]

    covered, view, all_hits = _execute_prefix(
        target, prefix, ir, trace_root, reset_hook, session_probe)
    target_covered = ckpt.target_block in covered
    verdicts = tuple(evaluate_session_conditions(g, view) for g in groups)
    conditions_verified = any(v.verdict == VERIFIED_TRUE for v in verdicts)
    observations = tuple(o for v in verdicts for o in v.observations)

    suffix_target = None
    suffix_verified = None
    if suffix:
        # Execute prefix+suffix on one fresh client, then judge suffix
        # separately from the already-verified prefix conditions.
        if reset_hook is not None:
            reset_hook()
        trace = TraceSession(root=trace_root)
        suffix_view = SessionSnapshotView("unknown")
        try:
            with trace:
                for step in prefix + suffix:
                    target(json.dumps(step, ensure_ascii=False,
                                      sort_keys=True).encode("utf-8"))
                suffix_view = _view_from_probe(session_probe)
        finally:
            if reset_hook is not None:
                reset_hook()
        suffix_hits = set(map_trace(trace.events, ir).block_hits)
        suffix_target = ckpt.target_block in suffix_hits
        suffix_verdicts = tuple(evaluate_session_conditions(g, suffix_view)
                                for g in groups)
        suffix_verified = any(v.verdict == VERIFIED_TRUE
                              for v in suffix_verdicts)

    if target_covered and conditions_verified:
        status = REPLAY_VERIFIED
    elif target_covered:
        status = REPLAY_TARGET_ONLY
    elif not view.authoritative:
        status = REPLAY_UNKNOWN
    else:
        status = REPLAY_FAILED
    return SessionReplayResult(
        checkpoint_id=ckpt.checkpoint_id, target_block=ckpt.target_block,
        status=status,
        replay_success=(status == REPLAY_VERIFIED),
        target_covered=target_covered,
        conditions_verified=conditions_verified,
        values=(dict(view.values) if view.values is not None else None),
        group_verdicts=verdicts, prefix_len=len(prefix),
        suffix_len=len(suffix), suffix_target_covered=suffix_target,
        suffix_conditions_verified=suffix_verified,
        trace_covered=all_hits, started_from_reset=reset_hook is not None,
        reason=("prefix-and-conditions" if status == REPLAY_VERIFIED
                else "target-without-verified-conditions"
                if status == REPLAY_TARGET_ONLY else "prefix-not-verified"),
        observations=observations)


def suffix_without_prefix_reaches_target(ckpt: SessionReplayCheckpoint,
                                         target, ir: ProgramIR, *,
                                         reset_hook=None, trace_root=None,
                                         suffix_steps: list[dict] | None = None
                                         ) -> bool:
    """Isolation control: the suffix alone must not inherit the prefix."""
    suffix = [dict(s) for s in (suffix_steps if suffix_steps is not None
                                else ckpt.suffix_steps())]
    if not suffix:
        return False
    if trace_root is None:
        blk = ir.blocks.get(ckpt.target_block)
        trace_root = Path(blk.file).parent if blk else None
    if reset_hook is not None:
        reset_hook()
    trace = TraceSession(root=trace_root)
    try:
        with trace:
            for step in suffix:
                target(json.dumps(step, ensure_ascii=False,
                                  sort_keys=True).encode("utf-8"))
    finally:
        if reset_hook is not None:
            reset_hook()
    hits = set(map_trace(trace.events, ir).block_hits)
    return ckpt.target_block in hits


def inject_session_checkpoint_seed(ckpt: SessionReplayCheckpoint,
                                   corpus_dir: str | Path) -> str | None:
    """Write the verified prefix as a C10 seed (content-hash dedup)."""
    corpus_dir = Path(corpus_dir)
    corpus_dir.mkdir(parents=True, exist_ok=True)
    data = ckpt.prefix_seed()
    cid = hashlib.sha256(data).hexdigest()[:16]
    path = corpus_dir / cid
    if path.exists():
        return None
    path.write_bytes(data)
    return cid
