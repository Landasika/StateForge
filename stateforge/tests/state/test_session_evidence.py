"""SS-07B: trusted evidence and normalized, run-scoped novelty."""

from __future__ import annotations

from stateforge.state.session_index import (COND_EXISTS, COND_TRUTHY,
                                            KEY_CONST, SessionCondition)
from stateforge.state.session_novelty import SessionStateNovelty
from stateforge.state.session_observation import (SessionSnapshotView,
                                                  build_session_evidence)


def _group(op=COND_EXISTS, value=None):
    return [SessionCondition(KEY_CONST, "uid", op, value=value,
                             guard_block="g", source_line=1)]


def test_raw_dict_and_empty_group_never_form_a_witness():
    raw = SessionSnapshotView("decoded", {"uid": 7}, client_id="c",
                              source="legacy-dict-probe", trusted=False)
    evidence = build_session_evidence(raw, [_group()], run_id="r",
                                      observer_id="flask-cookie-reader")
    assert evidence.trusted is False
    assert evidence.witness is None
    assert evidence.reason == "untrusted-observer"

    trusted = SessionSnapshotView("decoded", {"uid": 7}, client_id="c",
                                  source="flask-cookie", trusted=True,
                                  observer_id="flask-cookie-reader")
    empty = build_session_evidence(trusted, [], run_id="r",
                                   observer_id="flask-cookie-reader")
    assert empty.witness is None
    assert empty.trusted is False


def test_trusted_verified_evidence_is_client_and_point_bound():
    view = SessionSnapshotView("decoded", {"uid": 7}, client_id="client-a",
                               observation_point="after_response",
                               source="flask-cookie", trusted=True,
                               observer_id="flask-cookie-reader")
    ev = build_session_evidence(view, [_group()], run_id="r",
                                sequence_id="s1", step_index=2,
                                observer_id="flask-cookie-reader")
    assert ev.trusted is True
    assert ev.client_scope == "client-a"
    assert ev.observation_point_id == "after_response"
    assert ev.witness["verdict"] == "VERIFIED_TRUE"
    assert ev.predicate_results[0]["verdict"] == "VERIFIED_TRUE"


def test_random_cookie_value_changes_do_not_create_session_novelty():
    seen = SessionStateNovelty(run_id="r", baseline_id="b")
    groups = [_group()]
    first = seen.observe(
        SessionSnapshotView("decoded", {"uid": 1, "nonce": "aaa"},
                            client_id="c", source="flask-cookie",
                            observer_id="observer"),
        target_block="b1", groups=groups, sequence_id="s1")
    second = seen.observe(
        SessionSnapshotView("decoded", {"uid": 2, "nonce": "bbb"},
                            client_id="c", source="flask-cookie",
                            observer_id="observer"),
        target_block="b1", groups=groups, sequence_id="s2")
    assert first.new_verified_session_fact is True
    assert second.new_verified_session_fact is False
    assert first.after_fingerprint == second.after_fingerprint


def test_unknown_invalid_and_untrusted_are_not_verified_novelty():
    seen = SessionStateNovelty(run_id="r")
    groups = [[SessionCondition(KEY_CONST, "uid", COND_TRUTHY,
                                guard_block="g", source_line=1)]]
    for view in (
        SessionSnapshotView("invalid", None, source="flask-cookie"),
        SessionSnapshotView("unknown", None, source="flask-cookie"),
        SessionSnapshotView("decoded", {"uid": 7}, source="legacy-dict-probe",
                            trusted=False),
    ):
        ev = seen.observe(view, target_block="b1", groups=groups)
        assert ev.new_verified_session_fact is False
        assert ev.new_verified_transition is False


def test_transition_is_scoped_to_same_client_and_normalized():
    seen = SessionStateNovelty(run_id="r")
    groups = [_group()]
    before = SessionSnapshotView("absent", None, client_id="a",
                                 source="flask-cookie", observer_id="obs")
    after = SessionSnapshotView("decoded", {"uid": 7}, client_id="a",
                                source="flask-cookie", observer_id="obs")
    ev = seen.observe(after, target_block="b1", groups=groups,
                      client_role="a", before=before)
    assert ev.new_verified_transition is True
    again = seen.observe(after, target_block="b1", groups=groups,
                         client_role="a", before=before)
    assert again.new_verified_transition is False
    after_b = SessionSnapshotView("decoded", {"uid": 7}, client_id="b",
                                  source="flask-cookie", observer_id="obs")
    cross = seen.observe(after_b, target_block="b1", groups=groups,
                         client_role="b", before=before)
    assert cross.new_verified_transition is False


def test_novelty_dict_never_contains_raw_secret_values():
    seen = SessionStateNovelty(run_id="r")
    view = SessionSnapshotView("decoded", {"token": "super-secret"},
                               source="flask-cookie", observer_id="obs")
    ev = seen.observe(view, target_block="b1",
                      groups=[[SessionCondition(KEY_CONST, "token",
                                                COND_EXISTS,
                                                guard_block="g",
                                                source_line=1)]])
    text = str(ev.to_dict())
    assert "super-secret" not in text
