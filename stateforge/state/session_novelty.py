"""SS-07B: run-scoped, normalized Session state novelty.

The novelty key never uses raw Cookie bytes or raw secret values.  It uses:

* stable key presence;
* a coarse value abstraction (null/bool/number/string/container);
* the target condition signature and three-valued results;
* a before/after transition on the same client role.

Random signed cookies, regenerated session IDs, timestamps and nonces therefore
do not create new verified facts.  ``UNKNOWN`` never counts as a fact.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from stateforge.state.session_observation import (
    SESSION_STATUS_DECODED,
    SessionSnapshotView,
    VERIFIED_TRUE,
    VERIFIED_FALSE,
    UNKNOWN,
    evaluate_session_conditions,
)


def condition_signature(groups) -> str:
    payload = [[c.to_dict() if hasattr(c, "to_dict") else dict(c)
                for c in g] for g in groups]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return "cond-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _abstract_value(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool:true" if value else "bool:false"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (dict, list, tuple, set)):
        return "container"
    return "other"


def state_fingerprint(view: SessionSnapshotView) -> str:
    """Normalized fingerprint; deliberately loses raw value entropy."""
    if not view.authoritative or not view.trusted:
        return f"{view.status}:untrusted-or-nonauthoritative"
    values = view.values or {}
    normalized = {
        str(k): _abstract_value(v)
        for k, v in sorted(values.items(), key=lambda kv: str(kv[0]))
    }
    raw = json.dumps({
        "client_role": view.client_id,
        "status": view.status,
        "keys": normalized,
    }, ensure_ascii=False, sort_keys=True)
    return "sf-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class SessionNoveltyEvent:
    run_id: str
    sequence_id: str
    step_index: int
    target_block: str
    condition_id: str
    client_role: str
    status: str
    verdict: str
    new_verified_session_fact: bool = False
    new_verified_transition: bool = False
    new_proxy_progress: bool = False
    new_block: bool = False
    novelty_key: str = ""
    before_fingerprint: str = ""
    after_fingerprint: str = ""
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "sequence_id": self.sequence_id,
            "step_index": self.step_index,
            "target_block": self.target_block,
            "condition_id": self.condition_id,
            "client_role": self.client_role,
            "status": self.status,
            "verdict": self.verdict,
            "new_verified_session_fact": self.new_verified_session_fact,
            "new_verified_transition": self.new_verified_transition,
            "new_proxy_progress": self.new_proxy_progress,
            "new_block": self.new_block,
            "novelty_key": self.novelty_key,
            "before_fingerprint": self.before_fingerprint,
            "after_fingerprint": self.after_fingerprint,
            "reason": self.reason,
        }


@dataclass
class SessionStateNovelty:
    run_id: str
    baseline_id: str = ""
    _facts: set = field(default_factory=set)
    _transitions: set = field(default_factory=set)
    _proxy: set = field(default_factory=set)
    _blocks: set = field(default_factory=set)

    def observe_block(self, block_id: str) -> bool:
        if not block_id or block_id in self._blocks:
            return False
        self._blocks.add(block_id)
        return True

    def observe(self, view: SessionSnapshotView, *, target_block: str,
                groups, sequence_id: str = "", step_index: int = 0,
                client_role: str = "", before: SessionSnapshotView | None = None,
                ) -> SessionNoveltyEvent:
        role = client_role or view.client_id
        cid = condition_signature(groups)
        verdicts = [evaluate_session_conditions(g, view) for g in groups]
        winner = next((v for v in verdicts if v.verdict == VERIFIED_TRUE), None)
        verdict = winner.verdict if winner is not None else (
            verdicts[0].verdict if verdicts else UNKNOWN)
        after_fp = state_fingerprint(view)
        before_fp = state_fingerprint(before) if before is not None else ""

        if not view.trusted:
            return SessionNoveltyEvent(
                self.run_id, sequence_id, step_index, target_block, cid, role,
                view.status, UNKNOWN, after_fingerprint=after_fp,
                reason="untrusted-observer")
        if not view.authoritative:
            return SessionNoveltyEvent(
                self.run_id, sequence_id, step_index, target_block, cid, role,
                view.status, UNKNOWN, after_fingerprint=after_fp,
                reason=f"observation-{view.status}")

        # Only a fully true group is a verified fact.
        if winner is None:
            proxy_key = (target_block, cid, role, view.status, verdict)
            fresh = proxy_key not in self._proxy
            self._proxy.add(proxy_key)
            return SessionNoveltyEvent(
                self.run_id, sequence_id, step_index, target_block, cid, role,
                view.status, verdict, new_proxy_progress=fresh,
                novelty_key=str(proxy_key), before_fingerprint=before_fp,
                after_fingerprint=after_fp,
                reason="no-verified-group")

        values = view.values or {}
        normalized = tuple(sorted(
            (str(k), _abstract_value(v)) for k, v in values.items()))
        fact_key = (self.baseline_id, target_block, cid, role,
                    normalized, verdict)
        new_fact = fact_key not in self._facts
        self._facts.add(fact_key)
        new_transition = False
        transition_key = ""
        if (before is not None and before.trusted and before.authoritative
                and before.client_id == view.client_id):
            if before_fp != after_fp:
                transition_key = (role, before_fp, after_fp, cid)
                new_transition = transition_key not in self._transitions
                self._transitions.add(transition_key)
        return SessionNoveltyEvent(
            self.run_id, sequence_id, step_index, target_block, cid, role,
            view.status, verdict, new_verified_session_fact=new_fact,
            new_verified_transition=new_transition,
            novelty_key=str(fact_key), before_fingerprint=before_fp,
            after_fingerprint=after_fp,
            reason="verified" if new_fact else "already-seen")
