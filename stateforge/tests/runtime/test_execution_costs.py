"""SS-07A: physical request accounting must be recomputable."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stateforge.runtime.execution_costs import (BudgetExceeded,
                                               ExecutionCostLedger,
                                               read_costs)


class _Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        self.t += 0.25
        return self.t


def test_three_step_input_is_three_http_and_one_atheris_input(tmp_path):
    clock = _Clock()
    ledger = ExecutionCostLedger(out_dir=tmp_path, run_id="r1", arm="B",
                                 seed=7, request_limit=10, clock=clock)
    hits = []

    def target(data):
        hits.append(data)

    wrapped = ledger.wrap_target(target)
    ledger.begin_sequence("atheris-input")
    for i in range(3):
        wrapped(f"step{i}".encode())
    ledger.end_sequence()
    costs = ledger.finish()

    assert len(hits) == 3
    assert costs["target_http_requests"] == 3
    assert costs["sequence_attempts"] == 1
    assert costs["atheris_inputs"] == 1
    assert costs["elapsed_wall_seconds"] > 0
    recomputed = read_costs(tmp_path)
    assert recomputed["target_http_requests"] == 3
    assert recomputed["atheris_inputs"] == 1
    events = [json.loads(line) for line in
              (tmp_path / "per_request.jsonl").read_text().splitlines()]
    assert [e["step_index"] for e in events] == [1, 2, 3]
    assert all(e["event"] == "request" for e in events)


def test_403_and_exception_are_still_executed(tmp_path):
    ledger = ExecutionCostLedger(out_dir=tmp_path, run_id="r2", clock=_Clock())

    def target(data):
        if data == b"boom":
            raise RuntimeError("boom")
        return 403

    wrapped = ledger.wrap_target(target)
    ledger.begin_sequence()
    wrapped(b"403")
    with pytest.raises(RuntimeError):
        wrapped(b"boom")
    ledger.end_sequence(status="error")
    costs = ledger.finish()
    assert costs["target_http_requests"] == 2
    rows = [json.loads(line) for line in
            (tmp_path / "per_request.jsonl").read_text().splitlines()]
    assert rows[0]["status"] == "ok"
    assert rows[1]["status"] == "error"
    assert "boom" in rows[1]["error"]


def test_hard_limit_stops_before_target_call(tmp_path):
    ledger = ExecutionCostLedger(out_dir=tmp_path, run_id="r3",
                                 request_limit=2, clock=_Clock())
    calls = []

    def target(data):
        calls.append(data)

    wrapped = ledger.wrap_target(target)
    wrapped(b"1")
    wrapped(b"2")
    with pytest.raises(BudgetExceeded):
        wrapped(b"3")
    assert len(calls) == 2
    assert ledger.finish()["target_http_requests"] == 2
    blocked = [json.loads(line) for line in
               (tmp_path / "per_request.jsonl").read_text().splitlines()
               if "budget_blocked" in line]
    assert blocked and blocked[0]["event"] == "budget_blocked"


def test_phase_totals_add_up_and_reset_observer_are_counted(tmp_path):
    ledger = ExecutionCostLedger(out_dir=tmp_path, run_id="r4", clock=_Clock())
    target = ledger.wrap_target(lambda data: None, phase="bootstrap")
    target(b"a")
    target = ledger.wrap_target(lambda data: None, phase="checkpoint")
    target(b"b")
    target = ledger.wrap_target(lambda data: None, phase="prefix_replay")
    target(b"c")
    target = ledger.wrap_target(lambda data: None, phase="suffix")
    target(b"d")
    ledger.wrap_reset(lambda: None)()
    ledger.wrap_observer(lambda: {"ok": True})()
    ledger.scheduler_decision(reason="test")
    c = ledger.finish()
    assert c["bootstrap_http_requests"] == 1
    assert c["checkpoint_validation_http_requests"] == 1
    assert c["prefix_replay_http_requests"] == 1
    assert c["suffix_http_requests"] == 1
    assert c["reset_calls"] == 1
    assert c["observer_calls"] == 1
    assert c["scheduler_decisions"] == 1
    assert c["target_http_requests"] == 4


def test_fixed_mock_clock_totals_match_events(tmp_path):
    class Fixed:
        def __call__(self):
            return 5.0

    ledger = ExecutionCostLedger(out_dir=tmp_path, run_id="r5", clock=Fixed())
    w = ledger.wrap_target(lambda data: None)
    ledger.begin_sequence()
    w(b"a")
    w(b"b")
    ledger.end_sequence()
    first = ledger.costs_dict()
    second = ledger.finish()
    assert first["target_http_requests"] == second["target_http_requests"] == 2
    assert first["sequence_attempts"] == second["sequence_attempts"] == 1
    assert len((tmp_path / "per_request.jsonl").read_text().splitlines()) == 2


def test_9_bootstrap_2_validation_4_suffix_add_to_15(tmp_path):
    ledger = ExecutionCostLedger(out_dir=tmp_path, run_id="sum15",
                                 clock=_Clock())
    for phase, n in (("bootstrap", 9), ("checkpoint", 2), ("suffix", 4)):
        wrapped = ledger.wrap_target(lambda data: None, phase=phase)
        ledger.begin_sequence(phase)
        for _ in range(n):
            wrapped(b"x")
        ledger.end_sequence()
    c = ledger.finish()
    assert c["bootstrap_http_requests"] == 9
    assert c["checkpoint_validation_http_requests"] == 2
    assert c["suffix_http_requests"] == 4
    assert c["target_http_requests"] == 15
