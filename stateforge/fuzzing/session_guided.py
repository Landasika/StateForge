"""SS-03: structure-aware, session-guided multi-request seed search.

This is **not** a replacement fuzzer.  Atheris remains the only mutation/
execution engine.  This module is a bounded seed producer used:

* by tests, to demonstrate that a target block can be reached by composing
  independent single requests (no pre-built answer sequence); and
* by ``entry_driver --session-guided`` before ``atheris.Setup``, to place one
  structurally valid C10 sequence into the seed corpus.

Search contract
---------------
* ``request_pool`` is a list of *single* request dicts.  A dict containing
  ``steps`` is rejected: the answer sequence must be composed, not handed in.
* Every candidate executes from a clean client via ``reset_hook``; steps in
  one candidate share that client/Cookie Jar.
* Candidates are compared on real TraceSession→ProgramIR coverage.  Writer
  block coverage is search progress, never proof of a session condition.
* A witness is only reported when a trusted ``session_probe`` returns values
  that satisfy one full frontier condition group.  Without a probe the result
  is ``PROXY_PROGRESS``.
* Fixed seed, ``max_steps`` and ``max_execs`` bound the search; canonical
  sequence hashes prevent re-running the same failure.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from stateforge.core.ir import ProgramIR
from stateforge.fuzzing.entry_driver import encode_sequence_seed
from stateforge.runtime.mapper import map_trace
from stateforge.runtime.tracer import TraceSession
from stateforge.state.session_index import (COND_EQ, COND_EXISTS, COND_FALSY,
                                            COND_IN, COND_NE, COND_NOT_EXISTS,
                                            COND_NOT_IN, COND_TRUTHY,
                                            SessionCondition, SessionFrontier)
from stateforge.state.session_observation import (
    SESSION_STATUS_DECODED,
    UNKNOWN as SESSION_UNKNOWN,
    VERIFIED_FALSE,
    VERIFIED_TRUE,
    SessionSnapshotView,
    evaluate_session_conditions,
    snapshot_view,
)

STATUS_VERIFIED = "VERIFIED"
STATUS_PROXY = "PROXY_PROGRESS"
STATUS_NOT_FOUND = "NOT_FOUND"

# Typed boundary values only.  They are generic mutation operators, not target
# credentials: no "valid"/"wrong"/"admin" and no business-looking "7".
GENERIC_BOUNDARY_VALUES = (None, True, False, 0, 1, "")


@dataclass(frozen=True)
class ParameterCandidate:
    """One parameter-mutation value plus an auditable provenance label."""

    value: object
    source: str          # single-request-pool | generic-boundary | corpus

    def key(self) -> tuple[str, str]:
        # Keep 0 and False as distinct candidates even though Python compares
        # them equal.
        return (type(self.value).__name__, repr(self.value))


@dataclass(frozen=True)
class GuidedAttempt:
    index: int
    steps: tuple[dict, ...]
    sequence_hash: str
    covered_blocks: tuple[str, ...]
    writer_blocks: tuple[str, ...]
    target_covered: bool
    session_verified: bool | None
    distance: int | None
    score: float
    status: str
    session_values: dict | None = None
    session_verdict: str | None = None
    session_witness: dict | None = None
    trace_span: tuple[int, int] = (0, 0)
    step_spans: tuple[tuple[int, int], ...] = ()
    candidate_sources: tuple[str, ...] = ()
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "steps": [dict(s) for s in self.steps],
            "sequence_hash": self.sequence_hash,
            "covered_blocks": list(self.covered_blocks),
            "writer_blocks": list(self.writer_blocks),
            "target_covered": self.target_covered,
            "session_verified": self.session_verified,
            "distance": self.distance,
            "score": round(self.score, 4),
            "status": self.status,
            "session_values": self.session_values,
            "session_verdict": self.session_verdict,
            "session_witness": self.session_witness,
            "trace_span": list(self.trace_span),
            "step_spans": [list(x) for x in self.step_spans],
            "candidate_sources": list(self.candidate_sources),
            "error": self.error,
        }


@dataclass
class SessionGuidedResult:
    target_block: str
    guided: bool
    found: bool
    status: str
    seed: bytes | None
    steps: tuple[dict, ...]
    writer_blocks: tuple[str, ...]
    session_witness: dict | None
    attempts: list[GuidedAttempt] = field(default_factory=list)
    executions: int = 0
    dedup_skips: int = 0
    stop_reason: str = ""
    seed_value: int = 0
    max_steps: int = 0
    max_execs: int = 0
    isolated: bool = True

    @property
    def checkpoint_eligible(self) -> bool:
        return self.found and self.status == STATUS_VERIFIED

    def to_dict(self) -> dict:
        return {
            "target_block": self.target_block,
            "guided": self.guided,
            "found": self.found,
            "status": self.status,
            "checkpoint_eligible": self.checkpoint_eligible,
            "steps": [dict(s) for s in self.steps],
            "seed_sha256": (hashlib.sha256(self.seed).hexdigest()
                            if self.seed else None),
            "writer_blocks": list(self.writer_blocks),
            "session_witness": self.session_witness,
            "executions": self.executions,
            "dedup_skips": self.dedup_skips,
            "stop_reason": self.stop_reason,
            "seed_value": self.seed_value,
            "max_steps": self.max_steps,
            "max_execs": self.max_execs,
            "isolated": self.isolated,
            "attempts": [a.to_dict() for a in self.attempts],
        }


def _canonical_steps(steps: Iterable[dict]) -> str:
    return json.dumps(list(steps), ensure_ascii=False, sort_keys=True)


def _sequence_hash(target_block: str, guided: bool, steps: Iterable[dict]) -> str:
    payload = json.dumps({
        "target_block": target_block,
        "guided": guided,
        "steps": list(steps),
    }, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _coerce_probe_view(raw) -> SessionSnapshotView:
    """Adapt a probe result without losing its status.

    A ``SessionSnapshotView`` is already trusted.  A target snapshot is
    adapted by ``snapshot_view``.  A raw mapping is accepted only as an
    explicitly-labelled legacy test probe; production probes should return a
    view or the target's own snapshot.
    """
    if isinstance(raw, SessionSnapshotView):
        return raw
    if isinstance(raw, dict):
        return SessionSnapshotView(
            SESSION_STATUS_DECODED, dict(raw),
            observation_point="legacy-mapping-probe",
            source="legacy-mapping-probe")
    return snapshot_view(raw, observation_point="probe-snapshot",
                         source="probe-snapshot")


def group_satisfied(group: tuple[SessionCondition, ...],
                    view_or_values: SessionSnapshotView | dict | None
                    ) -> bool | None:
    """Compatibility wrapper around the authoritative group evaluator.

    Never returns True for an empty group, an unknown view, a missing key, or
    a condition group with an UNKNOWN component.
    """
    if view_or_values is None:
        return None
    view = _coerce_probe_view(view_or_values)
    verdict = evaluate_session_conditions(group, view)
    if verdict.verdict == VERIFIED_TRUE:
        return True
    if verdict.verdict == VERIFIED_FALSE:
        return False
    return None


class SessionGuidedSearch:
    def __init__(self, target: Callable[[bytes], None], ir: ProgramIR,
                 target_block: str, frontier: SessionFrontier,
                 request_pool: list[dict], *,
                 max_execs: int = 200, max_steps: int = 3,
                 seed: int = 0, guided: bool = True,
                 reset_hook: Callable[[], None] | None = None,
                 trace_root: str | Path | None = None,
                 session_probe: Callable[[], dict | None] | None = None,
                 beam_width: int = 6):
        if max_execs <= 0:
            raise ValueError("max_execs 必须 > 0")
        if max_steps <= 0:
            raise ValueError("max_steps 必须 > 0")
        if not request_pool:
            raise ValueError("request_pool 不能为空")
        for step in request_pool:
            if not isinstance(step, dict):
                raise ValueError(f"request_pool 项必须是 dict: {step!r}")
            if "steps" in step:
                raise ValueError("request_pool 不得包含完整序列种子(steps)")
        self.target = target
        self.ir = ir
        self.target_block = target_block
        self.frontier = frontier
        self.request_pool = [dict(s) for s in request_pool]
        self.max_execs = max_execs
        self.max_steps = max_steps
        self.seed_value = seed
        self.guided = guided
        self.reset_hook = reset_hook
        self.session_probe = session_probe
        self.beam_width = max(1, beam_width)
        if trace_root is None:
            blk = ir.blocks.get(target_block)
            trace_root = Path(blk.file).parent if blk else None
        self.trace_root = trace_root
        self._rng = random.Random(seed)
        self._seen: set[str] = set()
        self._attempts: list[GuidedAttempt] = []
        self._executions = 0
        self._dedup_skips = 0
        self._dist = self._distance_to_target()
        self._writer_blocks = set(frontier.candidate_blocks)
        self._value_pool = self._collect_values()
        self._single_scores: dict[str, float] = {}

    # ------------------------------------------------------------ execution

    def _distance_to_target(self) -> dict[str, int]:
        from collections import defaultdict, deque
        rev: dict[str, list[str]] = defaultdict(list)
        for edge in self.ir.edges:
            rev[edge.dst].append(edge.src)
        dist = {self.target_block: 0}
        q = deque([self.target_block])
        while q:
            cur = q.popleft()
            for prev in rev.get(cur, ()):
                if prev not in dist:
                    dist[prev] = dist[cur] + 1
                    q.append(prev)
        return dist

    def _reset(self) -> None:
        if self.reset_hook is not None:
            self.reset_hook()

    def _probe(self) -> SessionSnapshotView | None:
        if self.session_probe is None:
            return None
        try:
            return _coerce_probe_view(self.session_probe())
        except Exception:                          # noqa: BLE001
            return SessionSnapshotView(
                "unknown", observation_point="probe-exception",
                source="probe-exception")

    def _execute(self, steps: tuple[dict, ...]) -> GuidedAttempt:
        h = _sequence_hash(self.target_block, self.guided, steps)
        if h in self._seen:
            self._dedup_skips += 1
            raise _Duplicate(h)
        self._seen.add(h)
        self._executions += 1
        self._reset()
        error = None
        trace = TraceSession(root=self.trace_root)
        step_spans: list[tuple[int, int]] = []
        try:
            with trace:
                for step in steps:
                    start_ev = len(trace.events)
                    try:
                        self.target(json.dumps(step, ensure_ascii=False,
                                               sort_keys=True).encode("utf-8"))
                    except Exception as exc:       # noqa: BLE001
                        error = f"{type(exc).__name__}: {exc}"
                    step_spans.append((start_ev, len(trace.events)))
        finally:
            view = self._probe()
            self._reset()
        mapped = map_trace(trace.events, self.ir)
        covered = tuple(sorted(mapped.block_hits))
        writer_hits = tuple(sorted(self._writer_blocks & set(covered)))
        target_covered = self.target_block in covered
        witness = None
        session_verdict = None
        values = None
        if view is not None and view.authoritative:
            values = dict(view.values) if view.values is not None else {}
            for group in self.frontier.path_constraint_groups:
                verdict = evaluate_session_conditions(group, view)
                if verdict.verdict == VERIFIED_TRUE:
                    witness = {"values": dict(values), "conditions": [
                        c.to_dict() for c in group],
                        "verdict": verdict.to_dict()}
                    session_verdict = VERIFIED_TRUE
                    break
                if verdict.verdict == VERIFIED_FALSE:
                    session_verdict = VERIFIED_FALSE
                elif session_verdict is None:
                    session_verdict = SESSION_UNKNOWN
            if session_verdict is None:
                session_verdict = SESSION_UNKNOWN
        elif view is not None:
            session_verdict = SESSION_UNKNOWN
        if target_covered and witness is not None:
            status = STATUS_VERIFIED
        elif target_covered and view is not None and view.authoritative:
            status = "TARGET_REACHED_UNVERIFIED"
        elif target_covered:
            status = STATUS_PROXY
        else:
            status = "EXPLORE"
        dist = min((self._dist[b] for b in covered if b in self._dist),
                   default=None)
        score = self._score(target_covered, writer_hits, dist,
                            witness is not None, len(steps))
        attempt = GuidedAttempt(
            index=len(self._attempts) + 1, steps=steps, sequence_hash=h,
            covered_blocks=covered, writer_blocks=writer_hits,
            target_covered=target_covered,
            session_verified=(witness is not None) if view is not None and
            view.authoritative else None,
            distance=dist, score=score, status=status,
            session_values=values,
            session_verdict=session_verdict,
            session_witness=witness,
            trace_span=(0, len(trace.events)),
            step_spans=tuple(step_spans),
            candidate_sources=self.candidate_sources(steps), error=error)
        self._attempts.append(attempt)
        return attempt

    def _score(self, target: bool, writer_hits: tuple[str, ...],
               dist: int | None, verified: bool, n_steps: int) -> float:
        score = 0.0
        if target:
            score += 1000.0
        if verified:
            score += 500.0
        if self.guided:
            score += 100.0 * len(writer_hits)
        if dist is not None:
            score += 20.0 / (1.0 + dist)
        score -= 0.01 * n_steps
        return score

    # -------------------------------------------------------------- mutation

    def _collect_values(self) -> list[ParameterCandidate]:
        """Collect mutation values with an explicit source for each one.

        The engine must not know a target's valid credential.  It may reuse
        values that already appear in a legitimate single-request pool, plus
        type-boundary values that are generic to any parameter.  Callers can
        audit ``_value_pool`` / ``candidate_sources`` to see exactly where a
        candidate came from.
        """
        values: list[ParameterCandidate] = []
        seen: set[tuple[str, str]] = set()
        for step in self.request_pool:
            for section in ("query", "form", "json"):
                body = step.get(section)
                if isinstance(body, dict):
                    for value in body.values():
                        cand = ParameterCandidate(value, "single-request-pool")
                        if cand.key() not in seen:
                            seen.add(cand.key())
                            values.append(cand)
        for value in GENERIC_BOUNDARY_VALUES:
            cand = ParameterCandidate(value, "generic-boundary")
            if cand.key() not in seen:
                seen.add(cand.key())
                values.append(cand)
        return values

    def _value_candidate(self, value) -> ParameterCandidate:
        for cand in self._value_pool:
            if type(cand.value) is type(value) and cand.value == value:
                return cand
        return ParameterCandidate(value, "unknown")

    def candidate_sources(self, steps: tuple[dict, ...]) -> tuple[str, ...]:
        """Provenance for the parameter values present in a candidate."""
        out: list[str] = []
        for step in steps:
            for section in ("query", "form", "json"):
                body = step.get(section)
                if not isinstance(body, dict):
                    continue
                for value in body.values():
                    cand = self._value_candidate(value)
                    label = f"{section}:{cand.source}"
                    if label not in out:
                        out.append(label)
        return tuple(out)

    def _parameter_mutants(self, steps: tuple[dict, ...]) -> list[tuple[dict, ...]]:
        out: list[tuple[dict, ...]] = []
        if not steps:
            return out
        for idx, step in enumerate(steps):
            for section in ("query", "form", "json"):
                body = step.get(section)
                if not isinstance(body, dict) or not body:
                    continue
                for key in body:
                    for cand in self._value_pool:
                        value = cand.value
                        if type(value) is type(body[key]) and value == body[key]:
                            continue
                        new_body = dict(body)
                        new_body[key] = value
                        new_step = dict(step)
                        new_step[section] = new_body
                        new_steps = list(steps)
                        new_steps[idx] = new_step
                        out.append(tuple(new_steps))
        return out

    def _structural_mutants(self, steps: tuple[dict, ...]) -> list[tuple[dict, ...]]:
        out: list[tuple[dict, ...]] = []
        # append one independent single request
        if len(steps) < self.max_steps:
            for req in self.request_pool:
                out.append(steps + (dict(req),))
        # replace the last step
        if steps:
            for req in self.request_pool:
                out.append(steps[:-1] + (dict(req),))
        # delete one step (never empty)
        if len(steps) > 1:
            for i in range(len(steps)):
                out.append(steps[:i] + steps[i + 1:])
        return out

    # ----------------------------------------------------------------- run

    def run(self) -> SessionGuidedResult:
        singles = [(dict(req),) for req in self.request_pool]
        self._rng.shuffle(singles)
        scored: list[GuidedAttempt] = []
        for cand in singles:
            if self._executions >= self.max_execs:
                break
            try:
                attempt = self._execute(cand)
                scored.append(attempt)
                if len(cand) == 1:
                    self._single_scores[_canonical_steps(cand)] = attempt.score
            except _Duplicate:
                pass

        beam = sorted(scored, key=lambda a: a.score, reverse=True)[:self.beam_width]
        stop = "budget"
        while self._executions < self.max_execs:
            found = next((a for a in scored if self._terminal(a)), None)
            if found is not None:
                stop = "target"
                break
            expansions: list[tuple[dict, ...]] = []
            for attempt in beam:
                expansions.extend(self._structural_mutants(attempt.steps))
                expansions.extend(self._parameter_mutants(attempt.steps))
            if self.guided:
                # Estimate with the already observed single-request scores
                # (writer coverage/distance), never with a known answer.
                expansions.sort(
                    key=lambda cand: sum(
                        self._single_scores.get(_canonical_steps((step,)), 0.0)
                        for step in cand), reverse=True)
            else:
                self._rng.shuffle(expansions)
            progressed = False
            for cand in expansions:
                if self._executions >= self.max_execs:
                    stop = "budget"
                    break
                try:
                    attempt = self._execute(cand)
                except _Duplicate:
                    continue
                scored.append(attempt)
                progressed = True
                if self._terminal(attempt):
                    stop = "target"
                    break
            if stop == "target":
                break
            if not progressed:
                stop = "exhausted"
                break
            beam = sorted(scored, key=lambda a: a.score,
                          reverse=True)[:self.beam_width]

        best = next((a for a in reversed(scored) if self._terminal(a)), None)
        if best is None:
            best = max(scored, key=lambda a: a.score, default=None)
        if best is None:
            return SessionGuidedResult(
                target_block=self.target_block, guided=self.guided,
                found=False, status=STATUS_NOT_FOUND, seed=None, steps=(),
                writer_blocks=(), session_witness=None,
                attempts=self._attempts, executions=self._executions,
                dedup_skips=self._dedup_skips, stop_reason=stop,
                seed_value=self.seed_value, max_steps=self.max_steps,
                max_execs=self.max_execs)
        witness = best.session_witness if best.target_covered else None
        if best.target_covered and best.session_verified:
            status = STATUS_VERIFIED
        elif best.target_covered:
            status = STATUS_PROXY
        else:
            status = STATUS_NOT_FOUND
        seed = (encode_sequence_seed([dict(s) for s in best.steps])
                if best.target_covered else None)
        return SessionGuidedResult(
            target_block=self.target_block, guided=self.guided,
            found=best.target_covered, status=status, seed=seed,
            steps=best.steps, writer_blocks=best.writer_blocks,
            session_witness=witness, attempts=self._attempts,
            executions=self._executions, dedup_skips=self._dedup_skips,
            stop_reason=stop, seed_value=self.seed_value,
            max_steps=self.max_steps, max_execs=self.max_execs)

    def _terminal(self, attempt: GuidedAttempt) -> bool:
        if not attempt.target_covered:
            return False
        if self.session_probe is None:
            return True
        return attempt.session_verified is True


class _Duplicate(Exception):
    pass


def run_session_guided_search(target, ir: ProgramIR, target_block: str,
                              frontier: SessionFrontier,
                              request_pool: list[dict], **kwargs
                              ) -> SessionGuidedResult:
    return SessionGuidedSearch(target, ir, target_block, frontier,
                               request_pool, **kwargs).run()


# ------------------------------------------------------- CLI/bootstrap glue

def load_request_pool(path: str | Path) -> list[dict]:
    """Load a JSON list of independent single requests (no ``steps``)."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not all(isinstance(x, dict) for x in raw):
        raise ValueError("request pool 必须是 JSON list[dict]")
    if any("steps" in x for x in raw):
        raise ValueError("request pool 不得包含完整序列种子(steps)")
    return raw


def _module_session_probe(target_module):
    observe = getattr(target_module, "observe_session", None)
    if not callable(observe):
        return None

    def probe():
        snap = observe()
        return snapshot_view(
            snap, client_id="client-0", observation_point="after_response",
            source="module-observe_session")
    return probe


def bootstrap_session_guided(target_module, target_callable, ir: ProgramIR,
                             source_dir: str | Path, target_block: str,
                             request_pool: list[dict],
                             corpus_dir: str | Path, out_dir: str | Path, *,
                             max_steps: int = 3, max_execs: int = 200,
                             seed: int = 0, guided: bool = True,
                             session_probe=None) -> SessionGuidedResult:
    """Build index/frontier, run the bounded search, and inject one seed."""
    from stateforge.state.session_access import extract_session_accesses
    from stateforge.state.session_index import (build_session_index,
                                                query_session_frontier)

    source_dir = Path(source_dir)
    accesses = extract_session_accesses(source_dir, ir=ir)
    index = build_session_index(accesses)
    frontier = query_session_frontier(ir, index, target_block, accesses)
    probe = session_probe if session_probe is not None else \
        _module_session_probe(target_module)
    reset_hook = getattr(target_module, "reset_state", None)
    if not callable(reset_hook):
        reset_hook = None
    result = SessionGuidedSearch(
        target_callable, ir, target_block, frontier, request_pool,
        max_execs=max_execs, max_steps=max_steps, seed=seed, guided=guided,
        reset_hook=reset_hook, trace_root=source_dir,
        session_probe=probe).run()

    corpus = Path(corpus_dir)
    corpus.mkdir(parents=True, exist_ok=True)
    if result.seed:
        cid = hashlib.sha256(result.seed).hexdigest()[:16]
        (corpus / cid).write_bytes(result.seed)
        result.stop_reason = f"{result.stop_reason}:seed={cid}"
    audit = Path(out_dir) / "session_guided"
    audit.mkdir(parents=True, exist_ok=True)
    (audit / "result.json").write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=1),
        encoding="utf-8")
    (audit / "frontier.json").write_text(
        json.dumps(frontier.to_dict(), ensure_ascii=False, indent=1),
        encoding="utf-8")
    if callable(reset_hook):
        reset_hook()
    return result
