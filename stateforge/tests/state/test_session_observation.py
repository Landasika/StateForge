"""SS-04: runtime Session observation is verified, scoped and three-valued."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from stateforge.state.runtime_observer import (Evidence, StateEvidenceLedger,
                                               SCOPE_SESSION)
from stateforge.state.session_index import (COND_EQ, COND_EXISTS, COND_FALSY,
                                            COND_IN, COND_NE,
                                            COND_NOT_EXISTS, COND_NOT_IN,
                                            COND_TRUTHY, KEY_CONST,
                                            SessionCondition)
from stateforge.state.session_observation import (UNKNOWN, VERIFIED_FALSE,
                                                   VERIFIED_TRUE,
                                                   SessionSnapshotView,
                                                   evaluate_session_condition,
                                                   evaluate_session_conditions,
                                                   session_observations)

GATE = Path(__file__).resolve().parents[2] / "targets" / "session_gate"


def _cond(op, key="uid", value=None):
    return SessionCondition(key_kind=KEY_CONST, key_expr=key, op=op,
                            value=value, direction="true")


def _view(status="decoded", values=None, client="client-0"):
    return SessionSnapshotView(status=status, values=values, client_id=client)


def _load_target(name="sg_obs"):
    spec = importlib.util.spec_from_file_location(name, GATE / "fuzz_target.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_decoded_present_key_verifies_exists_truthy_and_eq():
    view = _view(values={"uid": 7})
    for cond in (_cond(COND_EXISTS), _cond(COND_TRUTHY), _cond(COND_EQ, value=7)):
        verdict = evaluate_session_condition(cond, view)
        assert verdict.verdict == VERIFIED_TRUE
        assert verdict.evidence is Evidence.RUNTIME_VERIFIED
        assert verdict.observation is not None
        assert verdict.observation.observed_value == 7
        assert verdict.observation.key.entity == "client-0"
        assert verdict.observation.key.scope == SCOPE_SESSION
        assert "after_response" in verdict.observation.source


def test_decoded_present_key_verifies_false_equality_and_falsy():
    view = _view(values={"uid": 7})
    assert evaluate_session_condition(_cond(COND_EQ, value=8), view).verdict == VERIFIED_FALSE
    assert evaluate_session_condition(_cond(COND_NE, value=7), view).verdict == VERIFIED_FALSE
    assert evaluate_session_condition(_cond(COND_FALSY), view).verdict == VERIFIED_FALSE
    assert evaluate_session_condition(_cond(COND_NOT_EXISTS), view).verdict == VERIFIED_FALSE


def test_absent_is_verified_absence_not_unknown():
    view = _view(status="absent")
    assert evaluate_session_condition(_cond(COND_EXISTS), view).verdict == VERIFIED_FALSE
    assert evaluate_session_condition(_cond(COND_NOT_EXISTS), view).verdict == VERIFIED_TRUE
    assert evaluate_session_condition(_cond(COND_TRUTHY), view).verdict == VERIFIED_FALSE
    assert evaluate_session_condition(_cond(COND_FALSY), view).verdict == VERIFIED_TRUE
    # session.get() would return None; equality to None is resolved, not guessed.
    assert evaluate_session_condition(_cond(COND_EQ, value=None), view).verdict == VERIFIED_TRUE


@pytest.mark.parametrize("status", ["invalid", "unknown"])
def test_invalid_or_unknown_cookie_never_becomes_absence(status):
    view = _view(status=status)
    for op in (COND_EXISTS, COND_NOT_EXISTS, COND_TRUTHY, COND_EQ):
        verdict = evaluate_session_condition(_cond(op, value=7), view)
        assert verdict.verdict == UNKNOWN
        assert verdict.evidence is Evidence.UNKNOWN
        assert verdict.observation is None
    assert session_observations(view) == []


def test_null_and_missing_value_kinds_are_distinct():
    present_null = evaluate_session_condition(_cond(COND_EXISTS),
                                              _view(values={"uid": None}))
    absent = evaluate_session_condition(_cond(COND_EXISTS), _view(status="absent"))
    assert present_null.verdict == VERIFIED_TRUE
    assert present_null.observation.value_kind == "NULL"
    assert present_null.observation.observed_value is None
    assert absent.verdict == VERIFIED_FALSE
    assert absent.observation.value_kind == "MISSING"
    # Present null is not "missing".
    assert present_null.observation.value_kind != "MISSING"


def test_membership_conditions_are_verified():
    view = _view(values={"uid": 7})
    assert evaluate_session_condition(_cond(COND_IN, value=[7, 8]), view).verdict == VERIFIED_TRUE
    assert evaluate_session_condition(_cond(COND_NOT_IN, value=[8]), view).verdict == VERIFIED_TRUE
    assert evaluate_session_condition(_cond(COND_IN, value=[8]), view).verdict == VERIFIED_FALSE


def test_group_is_and_of_conditions():
    view = _view(values={"uid": 7, "role": "admin"})
    good = evaluate_session_conditions(
        [_cond(COND_EXISTS), _cond(COND_EQ, value=7)], view)
    bad = evaluate_session_conditions(
        [_cond(COND_EXISTS), _cond(COND_EQ, value=8)], view)
    unknown = evaluate_session_conditions(
        [_cond(COND_EXISTS)],
        _view(status="invalid"))
    assert good.verdict == VERIFIED_TRUE
    assert bad.verdict == VERIFIED_FALSE
    assert unknown.verdict == UNKNOWN


def test_observations_are_bound_to_observation_point_and_client():
    view0 = _view(values={"uid": 7}, client="client-0")
    view1 = _view(values={"uid": 9}, client="client-1")
    obs0 = session_observations(view0)
    obs1 = session_observations(view1)
    assert obs0[0].key.entity == "client-0"
    assert obs1[0].key.entity == "client-1"
    assert obs0[0].key.render() != obs1[0].key.render()
    assert obs0[0].observed_value == 7
    assert obs1[0].observed_value == 9
    assert "client-0" in obs0[0].source


def test_ledger_records_verified_only_and_keeps_proxy_separate():
    ledger = StateEvidenceLedger(observer_name="session_cookie")
    view = _view(values={"uid": 7})
    novel = ledger.record(session_observations(view))
    assert len(novel) == 1
    assert ledger.verified_keys == {"Session.uid{client-0@session}"}
    # Re-recording the same fact is not novel.
    assert ledger.record(session_observations(view)) == []
    # UNKNOWN observations are never recorded as facts.
    assert ledger.record([]) == []
    ledger.record_proxy(["Session.uid"])
    payload = ledger.to_payload()
    assert payload["verified_atoms"] == ["Session.uid{client-0@session}"]
    assert payload["proxy_atoms"] == ["Session.uid{*@unknown}"]
    assert payload["verified_atoms"] != payload["proxy_atoms"]


def test_target_observer_is_read_only_and_does_not_trigger_routes():
    mod = _load_target("sg_obs_readonly")
    mod.reset_state()
    mod.target(b'{"method":"POST","path":"/authenticate","form":{"passcode":"valid"}}')
    hits_before = list(mod.target_hits())

    from stateforge.runtime.execution_context import observer_from_module
    observer = observer_from_module(mod)
    assert observer is not None
    observations = observer.observe()

    assert list(mod.target_hits()) == hits_before          # no route invoked
    assert any(o.key.render() == "Session.uid{client-0@session}"
               for o in observations)
    uid = next(o for o in observations
               if o.key.render() == "Session.uid{client-0@session}")
    assert uid.observed_value == 7
    assert uid.value_kind == "NUMBER"
    assert uid.evidence is Evidence.RUNTIME_VERIFIED


def test_target_observer_downgrades_invalid_cookie():
    mod = _load_target("sg_obs_invalid")
    mod.reset_state()
    mod.current_client().set_cookie("session", "not-a-valid-signature")
    assert mod.observe_session().status == "invalid"
    observer = mod.sf_state_observer()
    assert observer.observe() == []
    group = [_cond(COND_EXISTS)]
    verdict = observer.observe_conditions(group)
    assert verdict.verdict == UNKNOWN
    assert verdict.evidence is Evidence.UNKNOWN
