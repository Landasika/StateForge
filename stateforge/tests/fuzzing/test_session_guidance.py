"""SS-03: bounded, structure-aware Session-guided seed search."""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from stateforge.analysis.static.icfg import build_icfg
from stateforge.fuzzing.entry_driver import (decode_sequence_seed,
                                             dispatch_seed)
from stateforge.fuzzing.session_guided import (STATUS_NOT_FOUND, STATUS_PROXY,
                                               STATUS_VERIFIED,
                                               SessionGuidedSearch,
                                               load_request_pool)
from stateforge.state.session_access import extract_session_accesses
from stateforge.state.session_index import (build_session_index,
                                            query_session_frontier)

GATE = Path(__file__).resolve().parents[2] / "targets" / "session_gate"
TARGET = "app.py::protected::173-173::block::1"
WRITER = "app.py::authenticate::165-165::block::1"

POOL = [
    {"method": "POST", "path": "/authenticate", "form": {"passcode": "wrong"}},
    {"method": "POST", "path": "/authenticate", "form": {"passcode": "valid"}},
    {"method": "GET", "path": "/protected"},
    {"method": "GET", "path": "/protected_truthy"},
    {"method": "POST", "path": "/noise"},
    {"method": "POST", "path": "/logout"},
    {"method": "POST", "path": "/clear"},
]


def _load_target():
    spec = importlib.util.spec_from_file_location(
        "sg_target", GATE / "fuzz_target.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sg_target"] = mod
    spec.loader.exec_module(mod)
    return mod


def _frontier():
    ir = build_icfg(GATE)
    accesses = extract_session_accesses(GATE, ir=ir)
    index = build_session_index(accesses)
    return ir, accesses, query_session_frontier(ir, index, TARGET, accesses)


def _probe(mod):
    def probe():
        # Trusted view: status/client/observation-point survive the probe.
        return mod.session_view()
    return probe


def _search(mod, ir, frontier, pool, **kwargs):
    defaults = dict(max_execs=60, max_steps=3, seed=0, guided=True,
                    reset_hook=mod.reset_state, trace_root=GATE,
                    session_probe=_probe(mod), beam_width=6)
    defaults.update(kwargs)
    return SessionGuidedSearch(mod.target, ir, TARGET, frontier, pool,
                               **defaults).run()


def test_finds_two_step_sequence_from_independent_single_requests():
    mod = _load_target()
    ir, _accesses, frontier = _frontier()
    result = _search(mod, ir, frontier, POOL)

    assert result.found is True
    assert result.status == STATUS_VERIFIED
    assert result.checkpoint_eligible is True
    assert result.executions <= 60
    assert len(result.steps) == 2
    assert result.steps[0]["path"] == "/authenticate"
    assert result.steps[0]["form"]["passcode"] == "valid"
    assert result.steps[1] == {"method": "GET", "path": "/protected"}
    assert WRITER in result.writer_blocks
    assert result.session_witness["values"].get("uid") == 7

    target_attempt = next(a for a in result.attempts if a.target_covered)
    assert target_attempt.trace_span[1] > 0
    assert len(target_attempt.step_spans) == 2
    assert target_attempt.session_verified is True
    assert target_attempt.session_values == {"uid": 7}

    # The discovered seed is the frozen C10 format, not a new protocol.
    decoded = decode_sequence_seed(result.seed)
    assert decoded == [dict(s) for s in result.steps]


def test_request_pool_must_not_contain_a_prebuilt_sequence():
    mod = _load_target()
    ir, _accesses, frontier = _frontier()
    with pytest.raises(ValueError, match="steps"):
        SessionGuidedSearch(mod.target, ir, TARGET, frontier,
                            [{"steps": POOL[:2]}], reset_hook=mod.reset_state,
                            trace_root=GATE)


def test_json_pool_loader_rejects_steps(tmp_path):
    path = tmp_path / "pool.json"
    path.write_text(json.dumps([{"steps": POOL[:2]}]), encoding="utf-8")
    with pytest.raises(ValueError, match="steps"):
        load_request_pool(path)


def test_without_probe_target_is_proxy_only_not_checkpoint():
    mod = _load_target()
    ir, _accesses, frontier = _frontier()
    result = _search(mod, ir, frontier, POOL, session_probe=None)

    assert result.found is True
    assert result.status == STATUS_PROXY
    assert result.checkpoint_eligible is False
    assert result.session_witness is None
    assert all(a.session_verified is None for a in result.attempts)


def test_pool_without_auth_cannot_reach_gated_target():
    mod = _load_target()
    ir, _accesses, frontier = _frontier()
    pool = [
        {"method": "GET", "path": "/protected"},
        {"method": "POST", "path": "/noise"},
        {"method": "POST", "path": "/logout"},
        {"method": "POST", "path": "/clear"},
    ]
    result = _search(mod, ir, frontier, pool, max_execs=30)

    assert result.found is False
    assert result.status == STATUS_NOT_FOUND
    assert result.seed is None
    assert result.session_witness is None


def test_budget_and_sequence_dedup_are_enforced():
    mod = _load_target()
    ir, _accesses, frontier = _frontier()
    result = _search(mod, ir, frontier, POOL, max_execs=10, max_steps=2)

    assert result.executions <= 10
    assert len(result.attempts) <= 10
    hashes = [a.sequence_hash for a in result.attempts]
    assert len(hashes) == len(set(hashes))
    assert result.dedup_skips >= 0
    assert result.stop_reason in ("budget", "exhausted", "target")


def test_replay_on_fresh_client_and_second_search_are_isolated():
    mod = _load_target()
    ir, _accesses, frontier = _frontier()
    result = _search(mod, ir, frontier, POOL)
    assert result.seed

    mod.reset_state()
    dispatch_seed(mod.target, result.seed)
    assert sum(1 for h in mod.target_hits() if h.get("block") == "target_block") >= 1

    mod.reset_state()
    mod.target(json.dumps({"method": "GET", "path": "/protected"}).encode())
    assert sum(1 for h in mod.target_hits() if h.get("block") == "target_block") == 0

    again = _search(mod, ir, frontier, POOL)
    assert again.found is True
    assert again.status == STATUS_VERIFIED


def test_baseline_uses_same_pool_and_is_audited():
    mod = _load_target()
    ir, _accesses, frontier = _frontier()
    result = _search(mod, ir, frontier, POOL, guided=False)

    assert result.executions <= 60
    assert result.attempts
    assert result.guided is False
    # Baseline may or may not find it; the important contract is that it is
    # measured on the same pool and does not claim a verified checkpoint
    # without a witness.
    if result.found:
        assert result.status in (STATUS_VERIFIED, STATUS_PROXY)


def test_structure_mutation_appends_replaces_and_deletes():
    mod = _load_target()
    ir, _accesses, frontier = _frontier()
    search = SessionGuidedSearch(mod.target, ir, TARGET, frontier, POOL,
                                 max_steps=3, max_execs=5, seed=0,
                                 reset_hook=mod.reset_state, trace_root=GATE)
    seq = (POOL[0],)
    mutants = search._structural_mutants(seq)
    assert any(len(m) == 2 for m in mutants)          # append
    assert any(len(m) == 1 and m[0] != seq[0] for m in mutants)  # replace
    seq2 = (POOL[0], POOL[2])
    assert any(len(m) == 1 for m in search._structural_mutants(seq2))  # delete

    param = search._parameter_mutants((POOL[0],))
    assert param
    assert any(m[0]["form"]["passcode"] != "wrong" for m in param)


def test_no_target_specific_parameter_literals_are_injected():
    mod = _load_target()
    ir, _accesses, frontier = _frontier()
    search = SessionGuidedSearch(
        mod.target, ir, TARGET, frontier,
        [{"method": "POST", "path": "/authenticate",
          "form": {"passcode": "wrong"}}],
        max_execs=5, max_steps=2, seed=0, reset_hook=mod.reset_state,
        trace_root=GATE)

    values = [(c.value, type(c.value).__name__, c.source)
              for c in search._value_pool]
    assert not any(v == "valid" for v, _t, _s in values)
    assert not any(v == "admin" for v, _t, _s in values)
    assert not any(v == "7" for v, _t, _s in values)
    assert ("wrong", "str", "single-request-pool") in values
    assert (0, "int", "generic-boundary") in values
    assert (1, "int", "generic-boundary") in values
    assert all(s in ("single-request-pool", "generic-boundary")
               for _v, _t, s in values)


def test_wrong_credentials_only_do_not_get_mutated_into_a_valid_login():
    mod = _load_target()
    ir, _accesses, frontier = _frontier()
    pool = [
        {"method": "POST", "path": "/authenticate",
         "form": {"passcode": "wrong"}},
        {"method": "GET", "path": "/protected"},
    ]
    result = _search(mod, ir, frontier, pool, max_execs=40)

    assert result.found is False
    assert result.status == STATUS_NOT_FOUND
    assert result.seed is None
    assert result.stop_reason in ("budget", "exhausted")


def test_parameterized_passcode_and_business_neutral_routes():
    mod = _load_target()
    ir, _accesses, frontier = _frontier()
    app = mod._APP_MOD.create_app(
        passcode="gate-9f3a",
        routes={"authenticate": "/p1", "protected": "/p2"})
    holder = {"client": app.test_client()}

    def reset():
        holder["client"] = app.test_client()

    def target(data):
        step = json.loads(data)
        mod._dispatch_request(step, holder["client"])

    def probe():
        from stateforge.state.session_observation import snapshot_view
        snap = mod._APP_MOD.observe_session(holder["client"], app)
        return snapshot_view(snap, client_id="param-client",
                             observation_point="after_response",
                             source="parameterized-test")

    pool = [
        {"method": "POST", "path": "/p1",
         "form": {"passcode": "gate-9f3a"}},
        {"method": "GET", "path": "/p2"},
    ]
    result = SessionGuidedSearch(
        target, ir, TARGET, frontier, pool, max_execs=60, max_steps=3,
        seed=0, guided=True, reset_hook=reset, trace_root=GATE,
        session_probe=probe).run()

    assert result.found is True
    assert result.steps[0]["path"] == "/p1"
    assert result.steps[0]["form"]["passcode"] == "gate-9f3a"
    assert result.steps[1]["path"] == "/p2"
    assert any(s.startswith("form:single-request-pool")
               for a in result.attempts for s in a.candidate_sources)


def test_invalid_probe_never_becomes_verified_or_checkpoint():
    mod = _load_target()
    ir, _accesses, frontier = _frontier()
    from stateforge.state.session_observation import SessionSnapshotView
    result = _search(
        mod, ir, frontier, POOL,
        session_probe=lambda: SessionSnapshotView(
            "invalid", observation_point="bad-signature",
            source="test-invalid"))

    assert result.found is True
    assert result.status == STATUS_PROXY
    assert result.checkpoint_eligible is False
    assert result.session_witness is None
    assert all(a.session_verified is None for a in result.attempts)


def test_empty_condition_group_cannot_become_a_witness():
    mod = _load_target()
    ir, _accesses, frontier = _frontier()
    empty = dataclasses.replace(frontier, path_constraint_groups=())
    result = _search(mod, ir, empty, POOL)

    assert result.found is True
    assert result.status == STATUS_PROXY
    assert result.checkpoint_eligible is False
    assert result.session_witness is None


def test_probe_exception_is_unknown_and_keeps_a_reason():
    mod = _load_target()
    ir, _accesses, frontier = _frontier()

    def boom():
        raise RuntimeError("observer broke")

    result = _search(mod, ir, frontier, POOL, session_probe=boom)
    assert result.found in (True, False)
    assert result.checkpoint_eligible is False
    assert result.session_witness is None
