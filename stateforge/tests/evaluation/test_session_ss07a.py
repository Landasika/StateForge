"""SS-07A: fair accounting and no hidden D bootstrap."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from stateforge.evaluation import session_ss06
from stateforge.fuzzing.entry_driver import encode_sequence_seed

ROOT = Path(__file__).resolve().parents[3]
GATE = ROOT / "stateforge" / "targets" / "session_gate"


def test_d_does_not_bootstrap_and_all_costs_are_recomputable(tmp_path,
                                                             monkeypatch):
    calls = []
    original = session_ss06._bootstrap

    def spy(arm, *args, **kwargs):
        calls.append(arm)
        return original(arm, *args, **kwargs)

    monkeypatch.setattr(session_ss06, "_bootstrap", spy)
    out = tmp_path / "ss07a"
    session_ss06.run_protocol(
        seeds=(1,), seconds=1, total_target_exec_budget=30,
        arms=("STATE_GUIDED", "STATE_REUSE"), out_root=out)
    assert calls == ["STATE_GUIDED"]
    rows = [json.loads(line) for line in
            (out / "per_run.jsonl").read_text().splitlines()]
    by_arm = {r["arm"]: r for r in rows}
    assert by_arm["STATE_REUSE"]["bootstrap_http_requests"] == 0
    assert by_arm["STATE_REUSE"]["checkpoint_source_arm"] == "STATE_GUIDED"
    assert by_arm["STATE_REUSE"]["end_to_end_target_http_requests"] >= \
        by_arm["STATE_REUSE"]["target_http_requests"]
    for r in rows:
        assert r["elapsed_wall_seconds"] > 0
        assert r["target_http_requests"] <= 30
        ledger_dir = (out / "raw" / "seed1" / r["arm"] / "out" / "ledger")
        req = [json.loads(line) for line in
               (ledger_dir / "per_request.jsonl").read_text().splitlines()]
        actual = sum(1 for e in req if e["event"] == "request")
        if r["arm"] == "STATE_REUSE":
            assert actual == r["target_http_requests"]
        else:
            child = (out / "raw" / "seed1" / r["arm"] / "out" /
                     "atheris_cost" / "per_request.jsonl")
            child_n = sum(1 for e in
                          (json.loads(line) for line in child.read_text().splitlines())
                          if e["event"] == "request")
            assert actual + child_n == r["target_http_requests"]


def test_real_three_step_c10_is_three_http_one_atheris_input(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "three").write_bytes(encode_sequence_seed([
        {"method": "POST", "path": "/authenticate",
         "form": {"passcode": "valid"}},
        {"method": "GET", "path": "/protected"},
        {"method": "GET", "path": "/protected_truthy"},
    ]))
    out = tmp_path / "out"
    cost = tmp_path / "cost"
    cmd = [sys.executable, "-m", "stateforge.fuzzing.entry_driver",
           str(GATE / "fuzz_target.py"), str(tmp_path / "ir.json"),
           str(corpus), str(out), "--seconds", "1", "--mode", "off",
           "--seed", "1", "--max-runs", "1", "--cost-ledger",
           "--cost-out", str(cost), "--run-id", "three-step"]
    # The off mode does not read IR; an empty placeholder is enough.
    (tmp_path / "ir.json").write_text("{}")
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0, proc.stderr[-1000:]
    snap = json.loads((cost / "cost_snapshot.json").read_text())
    # libFuzzer may execute a corpus seed during initialization in addition to
    # the -runs input; every C10 input must still map to exactly 3 HTTP calls.
    assert snap["atheris_inputs"] >= 1
    sequences = [json.loads(line) for line in
                 (cost / "per_sequence.jsonl").read_text().splitlines()]
    end_steps = [e["steps"] for e in sequences
                 if e.get("event") == "sequence_end"]
    # Atheris may first execute an empty input; the C10 three-step seed must
    # still appear as one sequence with three requests, not three inputs.
    assert 3 in end_steps
    assert sum(end_steps) == snap["target_http_requests"]
