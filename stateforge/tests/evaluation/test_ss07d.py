"""SS-07D: freeze gate refuses incomplete/fairness-violating victories."""

from __future__ import annotations

import json

import pytest

from stateforge.evaluation.ss07d import (FreezeRefused, freeze,
                                         validate_controlled)


def _runs():
    out = []
    for i, arm in enumerate(("SINGLE", "SEQUENCE", "STATE_GUIDED",
                             "STATE_REUSE")):
        rec = {"arm": arm, "seed": 1, "target_http_requests": 1,
               "elapsed_wall_seconds": 0.1, "bootstrap_http_requests": 0,
               "session_condition_verified": True,
               "target_reached": arm == "STATE_REUSE",
               "failures": []}
        if arm == "STATE_REUSE":
            rec["checkpoint_id"] = "ckpt-1"
        if arm == "STATE_GUIDED":
            rec["online_summary"] = {"consumed_mutations": 2, "switches": 1}
        out.append(rec)
    return out


def _write_log(root, n=4):
    root.mkdir(parents=True, exist_ok=True)
    with (root / "per_request.jsonl").open("w") as fh:
        for _ in range(n):
            fh.write(json.dumps({"event": "request"}) + "\n")


def test_valid_controlled_run_passes_and_can_freeze(tmp_path):
    _write_log(tmp_path)
    runs = _runs()
    v = validate_controlled(tmp_path, runs, budget=2, seeds=(1,))
    assert v["ok"], v
    manifest = freeze(tmp_path, runs, budget=2, seeds=(1,),
                      source_paths=[__file__], test_command="pytest",
                      test_result="passed")
    assert manifest["frozen"] is True
    assert (tmp_path / "freeze_manifest.json").exists()


@pytest.mark.parametrize("mutation,check", [
    (lambda r: r["STATE_REUSE"].update(bootstrap_http_requests=1),
     "d_no_bootstrap"),
    (lambda r: r["STATE_GUIDED"]["online_summary"].update(
        consumed_mutations=0), "online_consumed_after_start"),
    (lambda r: r["SINGLE"].update(target_http_requests=3),
     "physical_request_budget"),
    (lambda r: r["STATE_REUSE"].update(checkpoint_id=""),
     "d_checkpoint_bound"),
    (lambda r: r["SEQUENCE"].update(elapsed_wall_seconds=0),
     "wall_clock_measured"),
])
def test_freeze_refuses_fairness_or_evidence_failures(tmp_path, mutation, check):
    _write_log(tmp_path)
    runs = {r["arm"]: r for r in _runs()}
    mutation(runs)
    v = validate_controlled(tmp_path, list(runs.values()), budget=2,
                            seeds=(1,))
    assert v["ok"] is False
    assert v["checks"][check]["ok"] is False
    with pytest.raises(FreezeRefused):
        freeze(tmp_path, list(runs.values()), budget=2, seeds=(1,),
               source_paths=[__file__], test_command="pytest",
               test_result="passed")
    assert not (tmp_path / "freeze_manifest.json").exists()
    assert (tmp_path / "freeze_refused.json").exists()


def test_root_log_mismatch_refuses_freeze(tmp_path):
    _write_log(tmp_path, n=3)
    with pytest.raises(FreezeRefused):
        freeze(tmp_path, _runs(), budget=2, seeds=(1,),
               source_paths=[__file__], test_command="pytest",
               test_result="passed")
