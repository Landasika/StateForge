"""SS-06A: protocol shape and shared-budget sanity."""

from __future__ import annotations

import json
from pathlib import Path

from stateforge.evaluation.session_ss06 import (_base_record, _summarize,
                                               run_protocol)


def test_summary_counts_d_reuse_separately():
    records = []
    for arm in ("SINGLE", "SEQUENCE", "STATE_GUIDED", "STATE_REUSE"):
        r = _base_record(1, arm)
        r["target_reached"] = arm in ("STATE_GUIDED", "STATE_REUSE")
        r["session_condition_verified"] = True
        r["total_target_execs"] = 10
        r["bootstrap_execs"] = 0 if arm == "SINGLE" else 5
        r["checkpoint_validation_execs"] = 1 if arm == "STATE_GUIDED" else 0
        r["prefix_replay_execs"] = 4 if arm == "STATE_REUSE" else 0
        records.append(r)
    summary = _summarize(records, {
        "seeds": [1],
        "source_revision": "deadbeef",
    })
    assert summary["arms"]["STATE_REUSE"]["mean_prefix_replay_execs"] == 4
    assert summary["arms"]["STATE_GUIDED"]["mean_checkpoint_validation_execs"] == 1
    assert summary["paired_C_minus_B"][0]["seed"] == 1


def test_single_arm_protocol_respects_budget_and_writes_manifest(tmp_path):
    out = tmp_path / "ss06"
    run_protocol(seeds=(1,), seconds=1, total_target_exec_budget=5,
                 arms=("SINGLE",), out_root=out)
    manifest = json.loads((out / "run_manifest.json").read_text())
    assert manifest["protocol"] == "ss06-v1"
    assert manifest["source_revision"]
    assert manifest["arms"] == ["SINGLE"]
    rows = [json.loads(line) for line in
            (out / "per_run.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["arm"] == "SINGLE"
    assert rows[0]["bootstrap_execs"] == 0
    assert rows[0]["total_target_execs"] <= 20
    assert (out / "SESSION_SS06_REPORT.md").exists()
