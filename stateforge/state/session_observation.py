"""SS-04: runtime Session observation → verified conditions (three-valued).

Observation contract
--------------------
* The provider reads the *client's Cookie Jar after the response* and decodes
  the Flask signed cookie read-only.  It never calls a route and never mutates
  the jar.
* ``decoded`` / ``absent`` can verify existence/absence for this observation
  point.  ``invalid`` / ``unknown`` are ``UNKNOWN`` — never silently treated
  as "absent" or "empty session".
* A verified condition produces a :class:`StateObservation` with
  ``Evidence.RUNTIME_VERIFIED``, an entity bound to the observing client, and
  a source string naming the observation point.  Proxy/coverage evidence is
  never upgraded here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

from stateforge.state.runtime_observer import (
    Evidence,
    OBSERVABLE,
    SCOPE_SESSION,
    StateKey,
    StateObservation,
    VALUE_KIND_BOOLEAN,
    VALUE_KIND_MISSING,
    VALUE_KIND_NULL,
    VALUE_KIND_NUMBER,
    VALUE_KIND_STRING,
    VALUE_KIND_UNKNOWN,
)
from stateforge.state.session_index import (
    COND_EQ,
    COND_EXISTS,
    COND_FALSY,
    COND_IN,
    COND_NE,
    COND_NOT_EXISTS,
    COND_NOT_IN,
    COND_TRUTHY,
    SessionCondition,
)

VERIFIED_TRUE = "VERIFIED_TRUE"
VERIFIED_FALSE = "VERIFIED_FALSE"
UNKNOWN = "UNKNOWN"

SESSION_STATUS_DECODED = "decoded"
SESSION_STATUS_ABSENT = "absent"
SESSION_STATUS_INVALID = "invalid"
SESSION_STATUS_UNKNOWN = "unknown"


def value_kind_of(value) -> str:
    if value is None:
        return VALUE_KIND_NULL
    if isinstance(value, bool):
        return VALUE_KIND_BOOLEAN
    if isinstance(value, (int, float)):
        return VALUE_KIND_NUMBER
    if isinstance(value, str):
        return VALUE_KIND_STRING
    return VALUE_KIND_UNKNOWN


@dataclass(frozen=True)
class SessionSnapshotView:
    """Immutable snapshot of one observation point (client + Cookie Jar)."""

    status: str
    values: dict | None = None
    client_id: str = "client-0"
    observation_point: str = "after_response"
    observed_at: int = -1
    source: str = ""

    @property
    def decoded(self) -> bool:
        return self.status == SESSION_STATUS_DECODED

    @property
    def absent(self) -> bool:
        return self.status == SESSION_STATUS_ABSENT

    @property
    def authoritative(self) -> bool:
        """Only decoded/absent may verify presence/absence."""
        return self.status in (SESSION_STATUS_DECODED, SESSION_STATUS_ABSENT)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "values": dict(self.values) if self.values is not None else None,
            "client_id": self.client_id,
            "observation_point": self.observation_point,
            "observed_at": self.observed_at,
            "source": self.source,
        }


def snapshot_view(snapshot, *, client_id: str = "client-0",
                  observation_point: str = "after_response",
                  observed_at: int = -1,
                  source: str = "target-snapshot") -> SessionSnapshotView:
    """Adapt the target's read-only ``SessionSnapshot`` to a view."""
    status = getattr(snapshot, "status", SESSION_STATUS_UNKNOWN)
    values = getattr(snapshot, "values", None)
    if status == SESSION_STATUS_DECODED and values is not None:
        values = dict(values)
    elif status == SESSION_STATUS_DECODED:
        # A decoded snapshot without values is not authoritative absence.
        status, values = SESSION_STATUS_UNKNOWN, None
    else:
        values = None
    return SessionSnapshotView(status=status, values=values,
                               client_id=client_id,
                               observation_point=observation_point,
                               observed_at=observed_at,
                               source=source)


def _source(view: SessionSnapshotView, cond: SessionCondition | None = None) -> str:
    where = f"session_cookie:{view.client_id}:{view.observation_point}"
    if cond is not None:
        where += f":guard={cond.guard_block or '?'}:line={cond.source_line}"
    return where


def condition_predicate(cond: SessionCondition) -> str:
    key = cond.key_expr if cond.key_kind == "CONST" else (
        "*" if cond.key_kind == "ANY" else f"?{cond.key_expr or 'unknown'}")
    base = f"Session.{key}"
    if cond.op == COND_EXISTS:
        return f"{base}.exists"
    if cond.op == COND_NOT_EXISTS:
        return f"{base}.not_exists"
    if cond.op == COND_TRUTHY:
        return f"{base}.truthy"
    if cond.op == COND_FALSY:
        return f"{base}.falsy"
    return f"{base} {cond.op} {json.dumps(cond.value, ensure_ascii=False)}"


def _observation(view: SessionSnapshotView, cond: SessionCondition,
                 verdict: str, present: bool, actual) -> StateObservation:
    if present:
        kind = value_kind_of(actual)
    else:
        kind = VALUE_KIND_MISSING
    return StateObservation(
        key=StateKey(predicate=condition_predicate(cond),
                     entity=view.client_id, scope=SCOPE_SESSION),
        value=(verdict == VERIFIED_TRUE),
        evidence=Evidence.RUNTIME_VERIFIED,
        source=_source(view, cond),
        observed_value=actual,
        value_kind=kind,
        observed_at=view.observed_at,
    )


@dataclass(frozen=True)
class SessionConditionVerdict:
    condition: SessionCondition
    verdict: str
    evidence: Evidence
    observation: StateObservation | None
    reason: str = ""

    @property
    def verified(self) -> bool:
        return self.verdict in (VERIFIED_TRUE, VERIFIED_FALSE)

    def to_dict(self) -> dict:
        return {
            "condition": self.condition.to_dict(),
            "verdict": self.verdict,
            "evidence": self.evidence.value,
            "reason": self.reason,
            "observation": (self.observation.to_dict()
                            if self.observation is not None else None),
        }


@dataclass(frozen=True)
class SessionGroupVerdict:
    verdict: str
    evidence: Evidence
    conditions: tuple[SessionConditionVerdict, ...] = ()
    observations: tuple[StateObservation, ...] = ()
    reason: str = ""

    @property
    def verified(self) -> bool:
        return self.verdict == VERIFIED_TRUE

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "evidence": self.evidence.value,
            "reason": self.reason,
            "conditions": [c.to_dict() for c in self.conditions],
            "observations": [o.to_dict() for o in self.observations],
        }


def evaluate_session_condition(cond: SessionCondition,
                               view: SessionSnapshotView) -> SessionConditionVerdict:
    if not view.authoritative:
        return SessionConditionVerdict(
            cond, UNKNOWN, Evidence.UNKNOWN, None,
            reason=f"observation-{view.status}")

    present = bool(view.decoded and view.values is not None
                   and cond.key_expr in view.values)
    actual = view.values.get(cond.key_expr) if present else None

    if cond.op == COND_EXISTS:
        truth = present
    elif cond.op == COND_NOT_EXISTS:
        truth = not present
    elif not present:
        # A missing key is not Python ``None``.  Equality/truthiness on a
        # missing key is unobservable unless the caller explicitly models a
        # ``get(key, default)`` default; that is a separate, opt-in feature.
        return SessionConditionVerdict(
            cond, UNKNOWN, Evidence.UNKNOWN, None,
            reason="missing-key")
    elif cond.op == COND_TRUTHY:
        truth = bool(actual)
    elif cond.op == COND_FALSY:
        truth = not bool(actual)
    elif cond.op == COND_EQ:
        truth = actual == cond.value
    elif cond.op == COND_NE:
        truth = actual != cond.value
    elif cond.op == COND_IN:
        try:
            truth = actual in cond.value
        except TypeError:
            return SessionConditionVerdict(
                cond, UNKNOWN, Evidence.UNKNOWN, None,
                reason="uncomparable-in")
    elif cond.op == COND_NOT_IN:
        try:
            truth = actual not in cond.value
        except TypeError:
            return SessionConditionVerdict(
                cond, UNKNOWN, Evidence.UNKNOWN, None,
                reason="uncomparable-not-in")
    else:
        return SessionConditionVerdict(
            cond, UNKNOWN, Evidence.UNKNOWN, None,
            reason=f"unsupported-condition:{cond.op}")

    verdict = VERIFIED_TRUE if truth else VERIFIED_FALSE
    obs = _observation(view, cond, verdict, present, actual)
    return SessionConditionVerdict(cond, verdict, Evidence.RUNTIME_VERIFIED,
                                   obs, reason="observed")


def evaluate_session_conditions(group: tuple[SessionCondition, ...] | list,
                                view: SessionSnapshotView) -> SessionGroupVerdict:
    verdicts = [evaluate_session_condition(c, view) for c in group]
    if not verdicts:
        return SessionGroupVerdict(UNKNOWN, Evidence.UNKNOWN, (),
                                   reason="empty-condition-group")
    if any(v.verdict == VERIFIED_FALSE for v in verdicts):
        return SessionGroupVerdict(VERIFIED_FALSE, Evidence.RUNTIME_VERIFIED,
                                   tuple(verdicts),
                                   tuple(v.observation for v in verdicts
                                         if v.observation is not None),
                                   reason="at-least-one-false")
    if any(v.verdict == UNKNOWN for v in verdicts):
        return SessionGroupVerdict(UNKNOWN, Evidence.UNKNOWN, tuple(verdicts),
                                   tuple(v.observation for v in verdicts
                                         if v.observation is not None),
                                   reason="at-least-one-unknown")
    return SessionGroupVerdict(VERIFIED_TRUE, Evidence.RUNTIME_VERIFIED,
                               tuple(verdicts),
                               tuple(v.observation for v in verdicts
                                     if v.observation is not None),
                               reason="all-true")


def session_observations(view: SessionSnapshotView) -> list[StateObservation]:
    """All present keys as verified existence facts (no absence inference)."""
    if not view.decoded or view.values is None:
        return []
    out: list[StateObservation] = []
    for key in sorted(view.values, key=str):
        value = view.values[key]
        out.append(StateObservation(
            key=StateKey(predicate=f"Session.{key}", entity=view.client_id,
                         scope=SCOPE_SESSION),
            value=True, evidence=Evidence.RUNTIME_VERIFIED,
            source=_source(view), observed_value=value,
            value_kind=value_kind_of(value), observed_at=view.observed_at))
    return out


class SessionRuntimeObserver:
    """RuntimeObserver protocol adapter over a read-only snapshot provider."""

    name = "session_cookie"
    subject_families = {"Session.*": OBSERVABLE}

    def __init__(self, provider: Callable[[], SessionSnapshotView],
                 client_id: str = "client-0"):
        self._provider = provider
        self.client_id = client_id

    def observe(self) -> list[StateObservation]:
        view = self._provider()
        if not isinstance(view, SessionSnapshotView):
            view = snapshot_view(view, client_id=self.client_id)
        return session_observations(view)

    def observe_conditions(self, group) -> SessionGroupVerdict:
        return evaluate_session_conditions(group, self._provider())
