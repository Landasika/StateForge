"""SS-04: Session facts never merge across clients or observation points."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from stateforge.state.runtime_observer import StateEvidenceLedger
from stateforge.state.session_index import COND_EXISTS, KEY_CONST, SessionCondition
from stateforge.state.session_observation import (UNKNOWN, SessionSnapshotView,
                                                   evaluate_session_condition,
                                                   session_observations)

GATE = Path(__file__).resolve().parents[2] / "targets" / "session_gate"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, GATE / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _cond():
    return SessionCondition(key_kind=KEY_CONST, key_expr="uid",
                            op=COND_EXISTS, direction="true")


def test_two_clients_do_not_share_verified_session_facts():
    app_mod = _load("sg_iso_app", "app.py")
    app = app_mod.create_app()
    a = app.test_client()
    b = app.test_client()

    a.post("/authenticate", data={"passcode": "valid"})
    assert app_mod.observe_session(a, app).values == {"uid": 7}
    assert app_mod.observe_session(b, app).status == "absent"
    assert b.get("/protected").status_code == 401

    view_a = SessionSnapshotView("decoded", {"uid": 7}, client_id="client-a",
                                 observation_point="after_response")
    view_b = SessionSnapshotView("absent", None, client_id="client-b",
                                 observation_point="after_response")
    assert evaluate_session_condition(_cond(), view_a).verdict == "VERIFIED_TRUE"
    assert evaluate_session_condition(_cond(), view_b).verdict == "VERIFIED_FALSE"

    obs_a = session_observations(view_a)
    obs_b = session_observations(view_b)
    assert obs_a and not obs_b
    assert obs_a[0].key.entity == "client-a"
    # No cross-client entity merge.
    assert all(o.key.entity != "client-b" for o in obs_a)


def test_reset_clears_cookie_jar_and_previous_observation():
    mod = _load("sg_iso_target", "fuzz_target.py")
    mod.reset_state()
    mod.target(b'{"method":"POST","path":"/authenticate","form":{"passcode":"valid"}}')
    assert mod.session_view().values == {"uid": 7}

    mod.reset_state()
    assert mod.observe_session().status == "absent"
    assert mod.session_view().status == "absent"
    mod.target(b'{"method":"GET","path":"/protected"}')
    assert sum(1 for h in mod.target_hits()
               if h.get("block") == "target_block") == 0


def test_invalid_cookie_is_unknown_and_does_not_erase_old_ledger():
    ledger = StateEvidenceLedger(observer_name="session_cookie")
    good = SessionSnapshotView("decoded", {"uid": 7}, client_id="client-0")
    ledger.record(session_observations(good))
    assert ledger.verified_keys == {"Session.uid{client-0@session}"}

    invalid = SessionSnapshotView("invalid", None, client_id="client-0")
    verdict = evaluate_session_condition(_cond(), invalid)
    assert verdict.verdict == UNKNOWN
    assert session_observations(invalid) == []
    # The old verified fact remains an old fact; the invalid observation did
    # not turn it into a current absence or a new positive.
    assert ledger.verified_keys == {"Session.uid{client-0@session}"}
    assert ledger.to_payload()["verified_atoms"] == [
        "Session.uid{client-0@session}"]


def test_observation_points_are_part_of_the_source_not_merged():
    before = SessionSnapshotView("absent", None, client_id="client-0",
                                 observation_point="before_response")
    after = SessionSnapshotView("decoded", {"uid": 7}, client_id="client-0",
                                observation_point="after_response")
    v_before = evaluate_session_condition(_cond(), before)
    v_after = evaluate_session_condition(_cond(), after)
    assert v_before.verdict == "VERIFIED_FALSE"
    assert v_after.verdict == "VERIFIED_TRUE"
    assert "before_response" in v_before.observation.source
    assert "after_response" in v_after.observation.source
    assert v_before.observation.key.render() == v_after.observation.key.render()
    # Same key, different observation point: observations are not collapsed
    # into one boolean.
    assert v_before.observation.to_dict() != v_after.observation.to_dict()


def test_sequence_replay_leaves_target_client_clean():
    mod = _load("sg_iso_replay", "fuzz_target.py")
    mod.reset_state()
    mod.target(b'{"steps":[{"method":"POST","path":"/authenticate",'
               b'"form":{"passcode":"valid"}},{"method":"GET",'
               b'"path":"/protected"}]}')
    assert mod.observe_session().status == "decoded"
    mod.reset_state()
    assert mod.observe_session().status == "absent"
