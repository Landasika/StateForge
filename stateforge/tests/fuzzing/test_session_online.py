"""SS-07C: scheduler consumes post-execution feedback, not startup order."""

from __future__ import annotations

import json

from stateforge.fuzzing.session_online import (OnlineSessionScheduler,
                                               SessionFrontierCandidate)
from stateforge.state.session_index import (COND_EXISTS, KEY_CONST,
                                            SessionCondition)
from stateforge.state.session_observation import SessionSnapshotView


class _Mapped:
    def __init__(self, blocks=()):
        self.block_hits = set(blocks)


class _Atheris:
    def Mutate(self, data, max_size):
        return b"mutated" if len(b"mutated") <= max_size else data[:max_size]


def _cand(writer, key):
    group = (SessionCondition(KEY_CONST, key, COND_EXISTS, guard_block=writer,
                              source_line=1),)
    return SessionFrontierCandidate("target", writer, key, "STATIC_MAY", group,
                                    group_id=writer)


def test_scheduler_switches_after_verified_feedback_and_mutator_consumes(tmp_path):
    a, b = _cand("w1", "uid"), _cand("w2", "uid")
    sched = OnlineSessionScheduler([a, b], request_pool=[
        {"method": "GET", "path": "/a"}], out_dir=tmp_path, run_id="r",
        target_block="target", seed=1, exploration_ratio=0)
    assert sched.current.writer_block == "w1"
    view = SessionSnapshotView("decoded", {"uid": 7}, source="flask-cookie",
                               observer_id="obs", trusted=True)
    sched.observe_execution(_Mapped({"target"}), view, sequence_id="s1")
    assert sched.current.writer_block == "w2"
    out = sched.mutate(b'{"steps":[{"method":"GET","path":"/a"}]}', 4096, 1,
                       _Atheris())
    assert isinstance(out, bytes)
    rows = [json.loads(line) for line in
            (tmp_path / "scheduler_online.jsonl").read_text().splitlines()]
    assert any(r["event"] == "frontier_switch" for r in rows)
    muts = [r for r in rows if r["event"] == "mutation_decision"]
    assert muts and muts[-1]["writer_block"] == "w2"
    assert muts[-1]["consumed"] is True


def test_untrusted_or_unknown_feedback_does_not_switch(tmp_path):
    sched = OnlineSessionScheduler([_cand("w1", "uid"), _cand("w2", "uid")],
                                   request_pool=[{"method": "GET",
                                                  "path": "/a"}],
                                   out_dir=tmp_path, run_id="r",
                                   target_block="target", seed=2)
    bad = SessionSnapshotView("decoded", {"uid": 7},
                              source="legacy-dict-probe", trusted=False)
    sched.observe_execution(_Mapped({"target"}), bad, sequence_id="s1")
    assert sched.current.writer_block == "w1"
    assert sched.candidates[0].verified_progress == 0
    assert sched.candidates[1].verified_progress == 0
    unknown = SessionSnapshotView("unknown", None, source="flask-cookie",
                                  observer_id="obs")
    sched.observe_execution(_Mapped({"target"}), unknown, sequence_id="s2")
    assert sched.current.writer_block == "w1"


def test_two_post_start_switches_are_logged(tmp_path):
    a, b = _cand("w1", "uid"), _cand("w2", "uid")
    sched = OnlineSessionScheduler([a, b], request_pool=[
        {"method": "GET", "path": "/a"}], out_dir=tmp_path, run_id="r",
        target_block="target", seed=3)
    v = SessionSnapshotView("decoded", {"uid": 7}, source="flask-cookie",
                            observer_id="obs")
    sched.observe_execution(_Mapped({"target"}), v, sequence_id="s1")
    assert sched.switches == 1
    # A second independent feedback event can switch focus again only after
    # the newly selected candidate is verified; the scheduler must not count
    # UNKNOWN as progress.
    sched.observe_execution(_Mapped({"target"}), v, sequence_id="s2")
    assert sched.switches >= 1
    assert sched.decisions >= 2
