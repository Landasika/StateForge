"""SS-07C: bounded online Session feedback for an Atheris campaign.

This module does not execute HTTP requests.  The driver's handler feeds it a
mapped trace and a trusted Session view after each real execution; the Atheris
custom mutator asks it for a focus and records why the next structure was
chosen.  Candidate writers still come from the existing static
``SessionAccessIndex`` / ``query_session_frontier`` output.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

from stateforge.fuzzing.entry_driver import decode_sequence_seed, encode_sequence_seed
from stateforge.state.session_observation import (SessionSnapshotView,
                                                  VERIFIED_TRUE,
                                                  evaluate_session_conditions)
from stateforge.state.session_novelty import SessionStateNovelty


def _gid(group) -> str:
    payload = json.dumps([c.to_dict() for c in group], sort_keys=True,
                         ensure_ascii=False)
    return "grp-" + hashlib.sha256(payload.encode()).hexdigest()[:12]


@dataclass
class SessionFrontierCandidate:
    target_block: str
    writer_block: str
    session_key_or_pattern: str
    source: str
    required_condition: object
    group_id: str = ""
    attempts: int = 0
    verified_progress: int = 0
    last_failure: str | None = None

    def to_dict(self) -> dict:
        return {
            "target_block": self.target_block,
            "writer_block": self.writer_block,
            "session_key_or_pattern": self.session_key_or_pattern,
            "source": self.source,
            "group_id": self.group_id,
            "attempts": self.attempts,
            "verified_progress": self.verified_progress,
            "last_failure": self.last_failure,
        }


class OnlineSessionScheduler:
    def __init__(self, candidates, *, request_pool, out_dir,
                 run_id="", target_block="", exploration_ratio=0.15,
                 max_steps=3, ledger=None, seed=0):
        self.candidates = list(candidates)
        self.request_pool = [dict(s) for s in request_pool]
        self.out = Path(out_dir)
        self.out.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.target_block = target_block
        self.exploration_ratio = exploration_ratio
        self.max_steps = max_steps
        self.ledger = ledger
        self.rng = random.Random(seed)
        self.novelty = SessionStateNovelty(run_id=run_id or "online")
        self.current: SessionFrontierCandidate | None = (
            self.candidates[0] if self.candidates else None)
        self.decisions = 0
        self.switches = 0
        self.mutations = 0
        self.consumed = 0
        self._last_writer = self.current.writer_block if self.current else ""
        self._write_summary()

    # ------------------------------------------------------------ feedback

    def _event(self, event: str, **fields) -> None:
        row = {"event": event, "run_id": self.run_id,
               "target_block": self.target_block,
               "timestamp": round(time.monotonic(), 6), **fields}
        with (self.out / "scheduler_online.jsonl").open(
                "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
        if self.ledger is not None:
            self.ledger.scheduler_decision(**{k: v for k, v in row.items()
                                              if k != "event"})

    def _write_summary(self) -> None:
        payload = {
            "run_id": self.run_id,
            "target_block": self.target_block,
            "candidates": [c.to_dict() for c in self.candidates],
            "decisions": self.decisions,
            "switches": self.switches,
            "mutations": self.mutations,
            "consumed_mutations": self.consumed,
            "current_writer": (self.current.writer_block
                               if self.current else None),
        }
        tmp = self.out / "online_summary.json.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(self.out / "online_summary.json")

    def observe_execution(self, mapped, view: SessionSnapshotView | None, *,
                          sequence_id: str = "", data: bytes = b"") -> None:
        blocks = set(getattr(mapped, "block_hits", ()) or ())
        if self.current is not None:
            self.current.attempts += 1
            if self.target_block in blocks:
                self.current.last_failure = None
        if view is not None:
            for cand in self.candidates:
                if not cand.required_condition:
                    continue
                if cand is not self.current and cand.writer_block not in blocks:
                    # Feedback belongs to the active focus; do not credit
                    # every candidate with the same condition.
                    continue
                try:
                    verdict = evaluate_session_conditions(
                        cand.required_condition, view)
                except Exception:                 # noqa: BLE001
                    continue
                if verdict.verdict == VERIFIED_TRUE and view.trusted:
                    cand.verified_progress += 1
                    novelty = self.novelty.observe(
                        view, target_block=cand.target_block,
                        groups=[cand.required_condition],
                        sequence_id=sequence_id)
                    self._event("state_novelty", **novelty.to_dict())
        self._select(sequence_id=sequence_id)

    def _select(self, *, sequence_id: str = "") -> None:
        if not self.candidates:
            return
        old = self.current
        # Move on after verified progress, or after repeated no-progress tries.
        ready = [c for c in self.candidates
                 if c is not old and c.verified_progress == 0]
        if old is not None and ready:
            if (old.verified_progress > 0
                    or old.attempts >= 3):
                self.current = ready[0]
        if self.current is not old:
            self.switches += 1
            self._event("frontier_switch",
                        from_writer=old.writer_block if old else None,
                        to_writer=self.current.writer_block,
                        target_block=self.current.target_block,
                        sequence_id=sequence_id,
                        reason=("verified-progress" if old and
                                old.verified_progress else "no-progress"))
        self.decisions += 1
        self._event("scheduler_decision",
                    writer_block=(self.current.writer_block
                                  if self.current else None),
                    evidence_level="STATIC_MAY",
                    reason="online-focus")
        self._write_summary()

    # ------------------------------------------------------------ mutator

    def propose_mutation_context(self) -> dict:
        explore = (not self.candidates
                   or self.rng.random() < self.exploration_ratio)
        return {
            "mode": "explore" if explore else "guided",
            "writer_block": (None if explore or self.current is None
                             else self.current.writer_block),
            "condition_group_id": (None if explore or self.current is None
                                   else self.current.group_id),
            "target_block": self.target_block,
        }

    def mutate(self, data: bytes, max_size: int, seed: int,
               atheris_module) -> bytes:
        context = self.propose_mutation_context()
        self.mutations += 1
        steps = decode_sequence_seed(data)
        try:
            import atheris
            if steps is None:
                out = atheris.Mutate(data, max_size)
            else:
                steps = [dict(s) for s in steps if isinstance(s, dict)]
                if not steps:
                    steps = [dict(self.rng.choice(self.request_pool))]
                if context["mode"] == "guided" and context["writer_block"]:
                    # Structural operation only: append/replace a generic
                    # request; never execute a request inside the mutator.
                    pick = dict(self.rng.choice(self.request_pool))
                    if len(steps) < self.max_steps:
                        steps.append(pick)
                    else:
                        steps[self.rng.randrange(len(steps))] = pick
                elif self.rng.random() < 0.5:
                    step = dict(self.rng.choice(steps))
                    for field in ("form", "query", "json"):
                        if isinstance(step.get(field), dict):
                            for k, v in list(step[field].items()):
                                if isinstance(v, str) and self.rng.random() < .5:
                                    step[field][k] = v + "x"
                    steps[self.rng.randrange(len(steps))] = step
                out = encode_sequence_seed(steps)
            if len(out) > max_size:
                out = out[:max_size]
            self.consumed += 1
            self._event("mutation_decision", **context,
                        consumed=True, result_bytes=len(out))
            self._write_summary()
            return out
        except Exception:                          # noqa: BLE001
            self._event("mutation_decision", **context, consumed=False,
                        error="mutator-fallback")
            return data


def scheduler_from_frontier(frontier, *, target_block: str,
                            request_pool, out_dir, run_id="", ledger=None,
                            max_steps=3, seed=0,
                            exploration_ratio=0.15) -> OnlineSessionScheduler:
    groups = list(getattr(frontier, "path_constraint_groups", ()) or ())
    candidates = []
    for i, writer in enumerate(getattr(frontier, "candidate_blocks", ()) or ()):
        group = groups[i] if i < len(groups) else (groups[0] if groups else [])
        candidates.append(SessionFrontierCandidate(
            target_block=target_block, writer_block=writer,
            session_key_or_pattern="|".join(
                str(getattr(c, "key_expr", "")) for c in group),
            source="STATIC_MAY", required_condition=tuple(group),
            group_id=_gid(group)))
    return OnlineSessionScheduler(
        candidates, request_pool=request_pool, out_dir=out_dir,
        run_id=run_id, target_block=target_block, max_steps=max_steps,
        ledger=ledger, seed=seed, exploration_ratio=exploration_ratio)
