"""SS-04: verified Session checkpoint + cold-start prefix replay."""

from __future__ import annotations

import dataclasses
import importlib.util
import sys
from pathlib import Path


from stateforge.analysis.static.icfg import build_icfg
from stateforge.fuzzing.entry_driver import decode_sequence_seed
from stateforge.fuzzing.session_guided import (STATUS_PROXY, STATUS_VERIFIED,
                                               SessionGuidedSearch)
from stateforge.state.session_access import extract_session_accesses
from stateforge.state.session_index import (COND_EQ, build_session_index,
                                            query_session_frontier)
from stateforge.state.session_observation import SessionSnapshotView
from stateforge.solving.session_replay import (REPLAY_TARGET_ONLY,
                                             REPLAY_VERIFIED,
                                             SessionReplayCheckpoint,
                                             checkpoint_from_guided_result,
                                             inject_session_checkpoint_seed,
                                             replay_session_checkpoint,
                                             suffix_without_prefix_reaches_target)

GATE = Path(__file__).resolve().parents[2] / "targets" / "session_gate"
TARGET = "app.py::protected::173-173::block::1"
POOL = [
    {"method": "POST", "path": "/authenticate", "form": {"passcode": "wrong"}},
    {"method": "POST", "path": "/authenticate", "form": {"passcode": "valid"}},
    {"method": "GET", "path": "/protected"},
    {"method": "GET", "path": "/protected_truthy"},
    {"method": "POST", "path": "/noise"},
]


def _load_target(name):
    spec = importlib.util.spec_from_file_location(name, GATE / "fuzz_target.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _probe(mod):
    def probe():
        snap = mod.observe_session()
        if snap.status == "decoded" and snap.values is not None:
            return dict(snap.values)
        return {}
    return probe


def _search(mod, ir, frontier, *, probe=True, pool=POOL, seed=0):
    return SessionGuidedSearch(
        mod.target, ir, TARGET, frontier, pool, max_execs=60, max_steps=3,
        seed=seed, guided=True, reset_hook=mod.reset_state, trace_root=GATE,
        session_probe=_probe(mod) if probe else None).run()


def _verified(name="sg_replay"):
    mod = _load_target(name)
    ir = build_icfg(GATE)
    accesses = extract_session_accesses(GATE, ir=ir)
    index = build_session_index(accesses)
    frontier = query_session_frontier(ir, index, TARGET, accesses)
    result = _search(mod, ir, frontier)
    assert result.status == STATUS_VERIFIED
    ckpt = checkpoint_from_guided_result(result, GATE / "app.py")
    assert ckpt is not None
    return mod, ir, ckpt


def test_only_verified_result_becomes_a_checkpoint():
    mod = _load_target("sg_replay_proxy")
    ir = build_icfg(GATE)
    accesses = extract_session_accesses(GATE, ir=ir)
    index = build_session_index(accesses)
    frontier = query_session_frontier(ir, index, TARGET, accesses)

    proxy = _search(mod, ir, frontier, probe=False)
    assert proxy.status == STATUS_PROXY
    assert checkpoint_from_guided_result(proxy, GATE / "app.py") is None

    failed = _search(mod, ir, frontier, pool=[
        {"method": "GET", "path": "/protected"},
        {"method": "POST", "path": "/noise"},
    ])
    assert failed.found is False
    assert checkpoint_from_guided_result(failed, GATE / "app.py") is None


def test_checkpoint_roundtrip_and_frozen_c10_seed(tmp_path):
    _mod, _ir, ckpt = _verified("sg_replay_roundtrip")
    assert len(ckpt.prefix_steps()) == 2
    assert ckpt.suffix_steps() == []
    assert decode_sequence_seed(ckpt.prefix_seed()) == ckpt.prefix_steps()
    assert decode_sequence_seed(ckpt.sequence_seed()) == ckpt.steps

    path = ckpt.save(tmp_path)
    loaded = SessionReplayCheckpoint.load(path)
    assert loaded == ckpt
    assert loaded.protocol_version == "session-checkpoint-v1"
    assert loaded.condition_objects()
    assert loaded.evidence
    assert loaded.evidence[0]["evidence"] == "runtime_verified"


def test_replay_on_clean_client_verifies_prefix_and_conditions():
    mod, ir, ckpt = _verified("sg_replay_clean")
    mod.reset_state()
    assert mod.observe_session().status == "absent"      # cold start

    result = replay_session_checkpoint(
        ckpt, mod.target, ir, reset_hook=mod.reset_state,
        session_probe=mod.session_view, trace_root=GATE)
    assert result.status == REPLAY_VERIFIED
    assert result.replay_success is True
    assert result.target_covered is True
    assert result.conditions_verified is True
    assert result.values == {"uid": 7}
    assert result.group_verdicts[0].verdict == "VERIFIED_TRUE"
    assert result.started_from_reset is True
    assert result.observations
    assert result.observations[0].evidence.value == "runtime_verified"


def test_replay_is_repeatable_from_cold_start():
    mod, ir, ckpt = _verified("sg_replay_repeat")
    for _ in range(2):
        mod.reset_state()
        assert mod.observe_session().status == "absent"
        result = replay_session_checkpoint(
            ckpt, mod.target, ir, reset_hook=mod.reset_state,
            session_probe=mod.session_view, trace_root=GATE)
        assert result.replay_success is True
        assert result.values == {"uid": 7}


def test_false_condition_is_target_only_not_false_replay_success():
    mod, ir, ckpt = _verified("sg_replay_bad_cond")
    cond = ckpt.condition_objects()[0][0]
    bad_cond = dataclasses.replace(cond, op=COND_EQ, value=999)
    bad = dataclasses.replace(ckpt, condition_groups=[[bad_cond.to_dict()]])

    result = replay_session_checkpoint(
        bad, mod.target, ir, reset_hook=mod.reset_state,
        session_probe=mod.session_view, trace_root=GATE)
    assert result.target_covered is True
    assert result.conditions_verified is False
    assert result.status == REPLAY_TARGET_ONLY
    assert result.replay_success is False


def test_missing_prefix_does_not_reach_target_or_claim_success():
    mod, ir, ckpt = _verified("sg_replay_no_prefix")
    broken = dataclasses.replace(
        ckpt, steps=[{"method": "GET", "path": "/protected"}],
        prefix_len=1, condition_groups=ckpt.condition_groups)
    result = replay_session_checkpoint(
        broken, mod.target, ir, reset_hook=mod.reset_state,
        session_probe=mod.session_view, trace_root=GATE)
    assert result.target_covered is False
    assert result.replay_success is False


def test_suffix_runs_after_prefix_but_alone_is_isolated():
    mod, ir, ckpt = _verified("sg_replay_suffix")
    suffix = [{"method": "GET", "path": "/protected"}]
    result = replay_session_checkpoint(
        ckpt, mod.target, ir, reset_hook=mod.reset_state,
        session_probe=mod.session_view, trace_root=GATE,
        suffix_steps=suffix)
    assert result.replay_success is True
    assert result.suffix_len == 1
    assert result.suffix_target_covered is True
    assert result.suffix_conditions_verified is True

    assert suffix_without_prefix_reaches_target(
        ckpt, mod.target, ir, reset_hook=mod.reset_state,
        trace_root=GATE, suffix_steps=suffix) is False


def test_unknown_observation_never_upgrades_to_verified():
    mod, ir, ckpt = _verified("sg_replay_unknown")
    result = replay_session_checkpoint(
        ckpt, mod.target, ir, reset_hook=mod.reset_state,
        session_probe=lambda: SessionSnapshotView("unknown"), trace_root=GATE)
    assert result.replay_success is False
    assert result.conditions_verified is False
    assert result.status in (REPLAY_TARGET_ONLY, "REPLAY_UNKNOWN")


def test_checkpoint_seed_injection_is_c10_and_deduplicated(tmp_path):
    _mod, _ir, ckpt = _verified("sg_replay_inject")
    cid = inject_session_checkpoint_seed(ckpt, tmp_path)
    assert cid is not None
    data = (tmp_path / cid).read_bytes()
    assert decode_sequence_seed(data) == ckpt.prefix_steps()
    assert inject_session_checkpoint_seed(ckpt, tmp_path) is None
