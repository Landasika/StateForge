"""SS-04/SS-05C: verified Session checkpoints and cold-start replay.

Two different things are recorded separately:

* **Target witness** — a full sequence reached the target and its Session
  conditions were verified.  Replaying it cold must reach the target again.
* **State preparation prefix** — a strictly shorter prefix establishes the
  Session conditions without needing the target suffix.  Different suffixes
  can be tried from the same prepared state, but the prefix is re-executed
  for every cold run (it is a request plan, not a physical memory snapshot).

A state-preparation checkpoint is never reported as ``REPLAY_VERIFIED``.
That status belongs to a replayed target witness only.
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
from stateforge.state.runtime_observer import StateObservation
from stateforge.state.session_index import SessionCondition
from stateforge.state.session_observation import (
    SESSION_STATUS_DECODED,
    SESSION_STATUS_UNKNOWN,
    SessionGroupVerdict,
    SessionSnapshotView,
    evaluate_session_conditions,
    session_observations,
    snapshot_view,
    VERIFIED_TRUE,
)

REPLAY_VERIFIED = "REPLAY_VERIFIED"          # target witness replayed
PREPARATION_VERIFIED = "PREPARATION_VERIFIED"
REPLAY_TARGET_ONLY = "TARGET_ONLY"
REPLAY_FAILED = "REPLAY_FAILED"
REPLAY_UNKNOWN = "REPLAY_UNKNOWN"

PREPARATION_FOUND = "FOUND"
PREPARATION_NONE = "NO_REUSABLE_PREFIX"
PREPARATION_UNVERIFIABLE = "UNVERIFIABLE"


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
    # SS-05C metadata.  Old v1 files load with these safe defaults.
    checkpoint_kind: str = "target_witness"
    baseline_id: str = "default"
    client_scope: str = "client-0"
    condition_group_id: str = ""
    source_sequence_hash: str = ""
    validation_observation_point: str = "after_prefix"
    verified_condition_witness: dict | None = None

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
            "checkpoint_kind": self.checkpoint_kind,
            "target_block": self.target_block,
            "source_file": self.source_file,
            "steps": self.steps,
            "prefix_len": self.prefix_len,
            "condition_groups": self.condition_groups,
            "session_values": self.session_values,
            "writer_blocks": self.writer_blocks,
            "evidence": self.evidence,
            "baseline_id": self.baseline_id,
            "client_scope": self.client_scope,
            "condition_group_id": self.condition_group_id,
            "source_sequence_hash": self.source_sequence_hash,
            "validation_observation_point": self.validation_observation_point,
            "verified_condition_witness": self.verified_condition_witness,
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
        # Old v1 files simply omit the SS-05C fields; defaults are safe and
        # are not treated as verified metadata.
        return cls(**d)

    def condition_objects(self) -> list[list[SessionCondition]]:
        return [[SessionCondition.from_dict(c) for c in group]
                for group in self.condition_groups]


@dataclass(frozen=True)
class PreparationPrefix:
    status: str
    steps: tuple[dict, ...] = ()
    prefix_len: int = 0
    view: SessionSnapshotView | None = None
    group_verdict: SessionGroupVerdict | None = None
    group_index: int | None = None
    covered_blocks: tuple[str, ...] = ()
    step_spans: tuple[tuple[int, int], ...] = ()
    reason: str = ""

    @property
    def found(self) -> bool:
        return self.status == PREPARATION_FOUND

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "steps": [dict(s) for s in self.steps],
            "prefix_len": self.prefix_len,
            "group_index": self.group_index,
            "covered_blocks": list(self.covered_blocks),
            "step_spans": [list(x) for x in self.step_spans],
            "reason": self.reason,
            "view": self.view.to_dict() if self.view is not None else None,
            "group_verdict": (self.group_verdict.to_dict()
                              if self.group_verdict is not None else None),
        }


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
    # SS-05C separate attribution fields.
    preparation_verified: bool = False
    target_witness_replayed: bool = False
    prefix_target_covered: bool = False
    whole_sequence_target_covered: bool = False
    suffix_incremental_blocks: tuple[str, ...] = ()
    prefix_covered: tuple[str, ...] = ()
    suffix_covered: tuple[str, ...] = ()
    prefix_step_spans: tuple[tuple[int, int], ...] = ()
    suffix_step_spans: tuple[tuple[int, int], ...] = ()

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
            "preparation_verified": self.preparation_verified,
            "target_witness_replayed": self.target_witness_replayed,
            "prefix_target_covered": self.prefix_target_covered,
            "whole_sequence_target_covered": self.whole_sequence_target_covered,
            "suffix_incremental_blocks": list(self.suffix_incremental_blocks),
            "prefix_covered": list(self.prefix_covered),
            "suffix_covered": list(self.suffix_covered),
            "prefix_step_spans": [list(x) for x in self.prefix_step_spans],
            "suffix_step_spans": [list(x) for x in self.suffix_step_spans],
        }


def _source_hash(steps: list[dict]) -> str:
    payload = json.dumps(steps, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _group_id(condition_groups: list[list[dict]]) -> str:
    payload = json.dumps(condition_groups, sort_keys=True, ensure_ascii=False)
    return "cond-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _baseline_id(target_block: str, source_file: str) -> str:
    payload = f"{target_block}|{source_file}".encode("utf-8")
    return "baseline-" + hashlib.sha256(payload).hexdigest()[:12]


def checkpoint_from_guided_result(result, source_file: str,
                                  *, prefix_len: int | None = None
                                  ) -> SessionReplayCheckpoint | None:
    """Create a full-sequence **target witness** checkpoint.

    This keeps the old SS-04 API.  A state-preparation checkpoint must be
    created by :func:`preparation_checkpoint_from_guided_result`, which
    independently validates a shorter prefix.
    """
    if not getattr(result, "checkpoint_eligible", False):
        return None
    if not result.session_witness or "values" not in result.session_witness:
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
                               observation_point="after_full_sequence",
                               source="guided-result")
    evidence = [o.to_dict() for o in session_observations(view)]
    return SessionReplayCheckpoint(
        checkpoint_id=f"sc-{hashlib.sha256(payload).hexdigest()[:12]}",
        target_block=result.target_block, source_file=str(source_file),
        steps=steps, condition_groups=groups, session_values=values,
        writer_blocks=list(result.writer_blocks), evidence=evidence,
        prefix_len=prefix, checkpoint_kind="target_witness",
        condition_group_id=_group_id(groups),
        source_sequence_hash=_source_hash(steps),
        validation_observation_point="after_full_sequence",
        verified_condition_witness=result.session_witness.get("verdict"))


def _view_from_probe(probe) -> SessionSnapshotView:
    if probe is None:
        return SessionSnapshotView(SESSION_STATUS_UNKNOWN,
                                   observation_point="no-probe",
                                   source="no-probe")
    try:
        raw = probe()
    except Exception as exc:                       # noqa: BLE001
        return SessionSnapshotView(
            SESSION_STATUS_UNKNOWN, observation_point="probe-exception",
            source=f"probe-exception:{type(exc).__name__}")
    if isinstance(raw, SessionSnapshotView):
        return raw
    if isinstance(raw, dict):
        # Explicitly labelled legacy adapter; it is not a target snapshot.
        return SessionSnapshotView(
            SESSION_STATUS_DECODED, dict(raw), client_id="client-0",
            observation_point="legacy-dict-probe",
            source="legacy-dict-probe")
    return snapshot_view(raw, client_id="client-0",
                         observation_point="after_prefix",
                         source="probe-snapshot")


def _map_span(events, start: int, end: int, ir: ProgramIR) -> set[str]:
    return set(map_trace(events[start:end], ir).block_hits)


def find_verified_preparation_prefix(
        steps: list[dict], condition_groups: list[list[SessionCondition]]
        | list[tuple[SessionCondition, ...]], target, ir: ProgramIR,
        target_block: str, *, reset_hook: Callable[[], None] | None,
        session_probe=None, trace_root=None) -> PreparationPrefix:
    """Find the earliest step boundary that establishes Session conditions.

    Only strictly shorter prefixes (``k < len(steps)``) are considered.  Each
    candidate starts from a fresh reset and is validated independently; the
    result is a request prefix, never a physical Cookie snapshot.
    """
    if reset_hook is None:
        return PreparationPrefix(PREPARATION_UNVERIFIABLE,
                                 reason="no-reset-hook")
    if len(steps) < 2:
        return PreparationPrefix(PREPARATION_NONE,
                                 reason="sequence-has-no-suffix")
    groups = [tuple(g) for g in condition_groups]
    if not groups:
        return PreparationPrefix(PREPARATION_NONE,
                                 reason="no-condition-group")
    if trace_root is None:
        blk = ir.blocks.get(target_block)
        trace_root = Path(blk.file).parent if blk else None

    had_authoritative = False
    for k in range(1, len(steps)):
        reset_hook()
        trace = TraceSession(root=trace_root)
        step_spans: list[tuple[int, int]] = []
        view = SessionSnapshotView(SESSION_STATUS_UNKNOWN,
                                   observation_point=f"after_prefix_{k}",
                                   source="preparation-prefix")
        errors: list[str] = []
        try:
            with trace:
                for step in steps[:k]:
                    start_ev = len(trace.events)
                    try:
                        target(json.dumps(step, ensure_ascii=False,
                                          sort_keys=True).encode("utf-8"))
                    except Exception as exc:           # noqa: BLE001
                        errors.append(f"{type(exc).__name__}: {exc}")
                    step_spans.append((start_ev, len(trace.events)))
                view = _view_from_probe(session_probe)
        finally:
            reset_hook()
        covered = tuple(sorted(set(map_trace(trace.events, ir).block_hits)))
        # A prefix that already reaches the target is not a reusable
        # preparation prefix: keep the full target witness instead.
        if target_block in covered:
            # No shorter prefix can be a *preparation* prefix if the first
            # step already reaches the target.  Still record whether the
            # observation was authoritative, so the final status distinguishes
            # "nothing reusable" from "we could not observe at all".
            had_authoritative = had_authoritative or view.authoritative
            continue
        if not view.authoritative:
            continue
        had_authoritative = True
        for gi, group in enumerate(groups):
            verdict = evaluate_session_conditions(group, view)
            if verdict.verdict == VERIFIED_TRUE:
                reason = "verified-prefix"
                if errors:
                    reason += f";request-errors={';'.join(errors)}"
                return PreparationPrefix(
                    PREPARATION_FOUND,
                    steps=tuple(dict(s) for s in steps[:k]),
                    prefix_len=k, view=view, group_verdict=verdict,
                    group_index=gi, covered_blocks=covered,
                    step_spans=tuple(step_spans), reason=reason)

    if had_authoritative:
        return PreparationPrefix(PREPARATION_NONE,
                                 reason="no-prefix-verifies-conditions")
    return PreparationPrefix(PREPARATION_UNVERIFIABLE,
                             reason="no-authoritative-observation")


def preparation_checkpoint_from_guided_result(
        result, source_file: str, ir: ProgramIR, *, target,
        reset_hook=None, session_probe=None, trace_root=None
        ) -> SessionReplayCheckpoint | None:
    """Create a checkpoint only from an independently verified prefix."""
    if not getattr(result, "checkpoint_eligible", False):
        return None
    if not result.session_witness:
        return None
    groups = [result.session_witness.get("conditions", [])]
    if not groups or not groups[0]:
        return None
    condition_groups = [[SessionCondition.from_dict(c) for c in groups[0]]]
    full_steps = [dict(s) for s in result.steps]
    prep = find_verified_preparation_prefix(
        full_steps, condition_groups, target, ir, result.target_block,
        reset_hook=reset_hook, session_probe=session_probe,
        trace_root=trace_root)
    if not prep.found or prep.view is None or prep.group_verdict is None:
        return None
    values = dict(prep.view.values or {})
    groups_out = [[c.to_dict() for c in condition_groups[0]]]
    evidence = [o.to_dict() for o in session_observations(prep.view)]
    source_file = str(source_file)
    return SessionReplayCheckpoint(
        checkpoint_id="sc-" + _source_hash(
            [dict(s) for s in prep.steps] + groups_out)[:12],
        target_block=result.target_block, source_file=source_file,
        steps=[dict(s) for s in prep.steps], condition_groups=groups_out,
        session_values=values, writer_blocks=list(result.writer_blocks),
        evidence=evidence, prefix_len=prep.prefix_len,
        protocol_version="session-checkpoint-v2",
        checkpoint_kind="state_preparation",
        baseline_id=_baseline_id(result.target_block, source_file),
        client_scope=prep.view.client_id,
        condition_group_id=_group_id(groups_out),
        source_sequence_hash=_source_hash(full_steps),
        validation_observation_point=prep.view.observation_point,
        verified_condition_witness=prep.group_verdict.to_dict())


def replay_session_checkpoint(ckpt: SessionReplayCheckpoint, target,
                              ir: ProgramIR, *, reset_hook=None,
                              session_probe=None, trace_root=None,
                              suffix_steps: list[dict] | None = None,
                              cost_ledger=None,
                              ) -> SessionReplayResult:
    """Replay a checkpoint from a cold client.

    Target-witness checkpoints execute their full sequence.  Preparation
    checkpoints execute their prefix, then (optionally) a suffix; suffix
    target coverage is measured only from the suffix trace span.
    """
    if trace_root is None:
        blk = ir.blocks.get(ckpt.target_block)
        trace_root = Path(blk.file).parent if blk else None
    groups = [tuple(g) for g in ckpt.condition_objects()]
    prefix = ckpt.prefix_steps()
    suffix = [dict(s) for s in (suffix_steps if suffix_steps is not None
                                else ckpt.suffix_steps())]
    is_preparation = ckpt.checkpoint_kind == "state_preparation"

    probe = session_probe
    if cost_ledger is not None:
        if reset_hook is not None:
            reset_hook = cost_ledger.wrap_reset(reset_hook)
        if probe is not None:
            probe = cost_ledger.wrap_observer(probe)

    if reset_hook is None:
        return SessionReplayResult(
            checkpoint_id=ckpt.checkpoint_id, target_block=ckpt.target_block,
            status=REPLAY_UNKNOWN, replay_success=False, target_covered=False,
            conditions_verified=False, values=None, prefix_len=len(prefix),
            suffix_len=len(suffix), reason="no-reset-hook",
            started_from_reset=False)

    # 1) Prefix alone, from a cold client.  This is the preparation check.
    reset_hook()
    prefix_trace = TraceSession(root=trace_root)
    prefix_view = SessionSnapshotView(SESSION_STATUS_UNKNOWN,
                                      observation_point="after_prefix",
                                      source="after_prefix")
    prefix_step_spans: list[tuple[int, int]] = []
    suffix_step_spans: list[tuple[int, int]] = []
    prefix_call = (cost_ledger.wrap_target(target, phase="checkpoint")
                   if cost_ledger is not None else target)
    if cost_ledger is not None:
        cost_ledger.begin_sequence("checkpoint-validation")
    try:
        with prefix_trace:
            for step in prefix:
                start_ev = len(prefix_trace.events)
                prefix_call(json.dumps(step, ensure_ascii=False,
                                       sort_keys=True).encode("utf-8"))
                prefix_step_spans.append((start_ev, len(prefix_trace.events)))
            prefix_view = _view_from_probe(probe)
    finally:
        cost_ledger.end_sequence() if cost_ledger is not None else None
        reset_hook()
    prefix_covered = tuple(sorted(set(map_trace(prefix_trace.events, ir).block_hits)))
    prefix_target = ckpt.target_block in prefix_covered
    verdicts = tuple(evaluate_session_conditions(g, prefix_view) for g in groups)
    conditions_verified = any(v.verdict == VERIFIED_TRUE for v in verdicts)
    observations = tuple(o for v in verdicts for o in v.observations)
    scope_ok = (not prefix_view.client_id
                or prefix_view.client_id == ckpt.client_scope)

    suffix_target = None
    suffix_verified = None
    suffix_covered: tuple[str, ...] = ()
    suffix_incremental: tuple[str, ...] = ()
    whole_target = prefix_target
    whole_covered = prefix_covered
    target_witness_replayed = False
    preparation_verified = False

    if is_preparation:
        preparation_verified = bool(conditions_verified and scope_ok)
        if suffix:
            # Execute prefix + suffix in one cold run and split the trace at
            # the prefix/suffix boundary.  History cannot contaminate suffix.
            reset_hook()
            trace = TraceSession(root=trace_root)
            suffix_view = SessionSnapshotView(SESSION_STATUS_UNKNOWN,
                                              observation_point="after_suffix",
                                              source="after_suffix")
            suffix_step_spans: list[tuple[int, int]] = []
            prep_prefix_call = (cost_ledger.wrap_target(target,
                                                        phase="prefix_replay")
                                if cost_ledger is not None else target)
            prep_suffix_call = (cost_ledger.wrap_target(target, phase="suffix")
                                if cost_ledger is not None else target)
            if cost_ledger is not None:
                cost_ledger.begin_sequence("prefix-suffix-replay")
            try:
                with trace:
                    for step in prefix:
                        prep_prefix_call(json.dumps(step, ensure_ascii=False,
                                                    sort_keys=True).encode("utf-8"))
                    boundary = len(trace.events)
                    for step in suffix:
                        start_ev = len(trace.events)
                        prep_suffix_call(json.dumps(step, ensure_ascii=False,
                                                    sort_keys=True).encode("utf-8"))
                        suffix_step_spans.append((start_ev,
                                                  len(trace.events)))
                    suffix_view = _view_from_probe(probe)
            finally:
                cost_ledger.end_sequence() if cost_ledger is not None else None
                reset_hook()
            suffix_covered = tuple(sorted(_map_span(
                trace.events, boundary, len(trace.events), ir)))
            prefix_in_run = _map_span(trace.events, 0, boundary, ir)
            suffix_incremental = tuple(sorted(set(suffix_covered)
                                              - set(prefix_covered)))
            suffix_target = ckpt.target_block in suffix_covered
            suffix_verdicts = tuple(evaluate_session_conditions(g, suffix_view)
                                    for g in groups)
            suffix_verified = any(v.verdict == VERIFIED_TRUE
                                  for v in suffix_verdicts)
            whole_covered = tuple(sorted(prefix_in_run | set(suffix_covered)))
            whole_target = ckpt.target_block in whole_covered
        if preparation_verified:
            status = PREPARATION_VERIFIED
        elif whole_target:
            status = REPLAY_TARGET_ONLY
        elif not prefix_view.authoritative:
            status = REPLAY_UNKNOWN
        else:
            status = REPLAY_FAILED
        replay_success = False
        reason = ("preparation-prefix-verified" if preparation_verified
                  else "preparation-prefix-not-verified")
    else:
        # Target witness: execute the full sequence (prefix + optional
        # suffix) and judge the whole sequence, not a preparation prefix.
        full = prefix + suffix
        reset_hook()
        trace = TraceSession(root=trace_root)
        full_view = SessionSnapshotView(SESSION_STATUS_UNKNOWN,
                                        observation_point="after_full_sequence",
                                        source="after_full_sequence")
        boundary = 0
        suffix_step_spans = []
        witness_prefix_call = (cost_ledger.wrap_target(target,
                                                       phase="prefix_replay")
                              if cost_ledger is not None else target)
        witness_suffix_call = (cost_ledger.wrap_target(target, phase="suffix")
                               if cost_ledger is not None else target)
        if cost_ledger is not None:
            cost_ledger.begin_sequence("target-witness")
        try:
            with trace:
                for step in prefix:
                    witness_prefix_call(json.dumps(step, ensure_ascii=False,
                                                   sort_keys=True).encode("utf-8"))
                boundary = len(trace.events)
                for step in suffix:
                    start_ev = len(trace.events)
                    witness_suffix_call(json.dumps(step, ensure_ascii=False,
                                                   sort_keys=True).encode("utf-8"))
                    suffix_step_spans.append((start_ev, len(trace.events)))
                full_view = _view_from_probe(probe)
        finally:
            cost_ledger.end_sequence() if cost_ledger is not None else None
            reset_hook()
        full_covered = tuple(sorted(set(map_trace(trace.events, ir).block_hits)))
        whole_covered = full_covered
        whole_target = ckpt.target_block in full_covered
        if suffix:
            prefix_covered = tuple(sorted(_map_span(
                trace.events, 0, boundary, ir)))
            prefix_target = ckpt.target_block in prefix_covered
            suffix_covered = tuple(sorted(_map_span(
                trace.events, boundary, len(trace.events), ir)))
            suffix_incremental = tuple(sorted(set(suffix_covered)
                                              - set(prefix_covered)))
            suffix_target = ckpt.target_block in suffix_covered
            suffix_verified = bool(full_view.authoritative and
                                   any(evaluate_session_conditions(g, full_view).verdict
                                       == VERIFIED_TRUE for g in groups))
        else:
            prefix_covered = full_covered
            prefix_target = whole_target
        full_verdicts = tuple(evaluate_session_conditions(g, full_view)
                              for g in groups)
        conditions_verified = any(v.verdict == VERIFIED_TRUE
                                  for v in full_verdicts)
        observations = tuple(o for v in full_verdicts for o in v.observations)
        target_witness_replayed = bool(whole_target and conditions_verified)
        if target_witness_replayed:
            status = REPLAY_VERIFIED
        elif whole_target:
            status = REPLAY_TARGET_ONLY
        elif not full_view.authoritative:
            status = REPLAY_UNKNOWN
        else:
            status = REPLAY_FAILED
        replay_success = target_witness_replayed
        prefix_view = full_view
        reason = ("target-witness-replayed" if target_witness_replayed
                  else "target-witness-not-replayed")

    if not scope_ok:
        status = REPLAY_UNKNOWN
        replay_success = False
        preparation_verified = False
        target_witness_replayed = False
        reason = "client-scope-mismatch"

    return SessionReplayResult(
        checkpoint_id=ckpt.checkpoint_id, target_block=ckpt.target_block,
        status=status, replay_success=replay_success,
        target_covered=whole_target,
        conditions_verified=conditions_verified,
        values=(dict(prefix_view.values)
                if prefix_view.values is not None else None),
        group_verdicts=verdicts, prefix_len=len(prefix),
        suffix_len=len(suffix), suffix_target_covered=suffix_target,
        suffix_conditions_verified=suffix_verified,
        trace_covered=whole_covered, started_from_reset=True,
        reason=reason, observations=observations,
        preparation_verified=preparation_verified,
        target_witness_replayed=target_witness_replayed,
        prefix_target_covered=prefix_target,
        whole_sequence_target_covered=whole_target,
        suffix_incremental_blocks=suffix_incremental,
        prefix_covered=prefix_covered, suffix_covered=suffix_covered,
        prefix_step_spans=tuple(prefix_step_spans),
        suffix_step_spans=tuple(suffix_step_spans))


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
