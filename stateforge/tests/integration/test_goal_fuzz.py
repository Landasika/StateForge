"""PR-3: actual Atheris consumption and state isolation at input boundaries."""

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from stateforge.core.seeds import encode_sequence_seed
from stateforge.pipeline import GoalRunner
from stateforge.pipeline.goal_fuzz import fuzz_checkpoint

SHOP = Path(__file__).resolve().parents[2] / "targets" / "stateful"
BLOCK = "t_state_shop.py::_handle::91-92::block::1"


def test_generated_checkpoint_is_consumed_by_real_atheris(tmp_path):
    if importlib.util.find_spec("atheris") is None:
        pytest.skip("requires stateforge[fuzz]")
    report = GoalRunner().run(SHOP, BLOCK, source="t_state_shop.py", mode="fuzz",
                             fuzz_seconds=1, fuzz_runs=20, out=tmp_path)
    assert report.success, report.summary
    fuzz = report.summary["fuzzing"]
    assert fuzz["verified"] and fuzz["consumed"] and fuzz["target_covered"]
    assert fuzz["returncode"] == 0
    assert "--lifecycle" in fuzz["command"] and "reset" in fuzz["command"]
    seed = next((Path(report.summary["checkpoint"]).parent.parent / "corpus").glob(fuzz["seed_id"]))
    assert hashlib.sha256(seed.read_bytes()).hexdigest() == fuzz["seed_sha256"]
    receipts = [json.loads(line) for line in Path(fuzz["receipts"]).read_text().splitlines()]
    assert any(r["sha256"] == fuzz["seed_sha256"] and r["completed_steps"] == 3
               and r["target_covered"] and r["isolation"] == "reset"
               and r["error"] is None for r in receipts)
    assert "DONE" in Path(fuzz["log"]).read_text()


def test_sequence_shares_state_but_next_fuzz_input_is_isolated(tmp_path):
    from stateforge.analysis.static.icfg import build_icfg
    from stateforge.fuzzing.candidates import CandidateCollector
    from stateforge.fuzzing.entry_driver import dispatch_seed
    from stateforge.fuzzing.feedback.structural import StructuralFeedback
    from stateforge.fuzzing.recorder import ExperimentRecorder
    from stateforge.runtime.execution_context import ExecutionContext, ResetLifecycle
    from stateforge.runtime.plan_executor import load_target_module
    from stateforge.runtime.tracer import TraceSession

    report = GoalRunner().run(SHOP, BLOCK, source="t_state_shop.py", mode="plan")
    full = encode_sequence_seed([{"json": s["json"]} for s in report.plan["steps"]])
    lone_ship = encode_sequence_seed([{"json": {"op": "ship_order"}}])
    mod = load_target_module(SHOP / "t_state_shop.py")
    resets = []

    def reset():
        resets.append(1)
        mod.reset_state()

    ir = build_icfg(SHOP)
    collector = CandidateCollector(ir, TraceSession(root=SHOP), StructuralFeedback(), None,
                                   ExperimentRecorder(tmp_path, "isolation", 0), 0,
                                   execution=ExecutionContext(ResetLifecycle([reset])))
    dispatch = lambda data: dispatch_seed(mod.target, data)
    collector.handle(dispatch, full)
    assert BLOCK in collector.last_mapped.blocks
    assert mod.HITS == ["register_ok", "order_created", "ship_privileged"]
    assert len(resets) == 1  # One reset per input, never between the three requests.
    collector.handle(dispatch, lone_ship)
    assert BLOCK not in collector.last_mapped.blocks
    assert mod.HITS == ["ship_denied"] and mod._SESSION == {}
    collector.handle(dispatch, full)
    assert BLOCK in collector.last_mapped.blocks and len(resets) == 3


def test_corpus_file_alone_does_not_prove_fuzzer_consumption(tmp_path):
    report = GoalRunner().run(SHOP, BLOCK, source="t_state_shop.py", out=tmp_path / "run")
    checkpoint = Path(report.summary["checkpoint"])
    with patch("stateforge.pipeline.goal_fuzz.subprocess.run",
               return_value=subprocess.CompletedProcess([], 0)):
        fuzz = fuzz_checkpoint(checkpoint, checkpoint.parent.parent / "ir.json", tmp_path / "fuzz",
                               seconds=1, max_runs=1, run_id="no-callback")
    assert fuzz["seed_id"]
    assert not fuzz["consumed"] and not fuzz["verified"]


def test_fuzz_failure_is_reported_separately_from_verified_checkpoint(tmp_path):
    with patch("stateforge.pipeline.goal_runner.fuzz_checkpoint", return_value={
        "verified": False, "consumed": False, "error": "Atheris dependency missing"
    }):
        report = GoalRunner().run(SHOP, BLOCK, source="t_state_shop.py", mode="fuzz", out=tmp_path)
    assert not report.success and not report.summary["verified"]
    assert report.summary["failed_stage"] == "fuzzing"
    assert report.summary["execution_verified"]
    assert Path(report.summary["checkpoint"]).is_file()


@pytest.mark.parametrize("seconds,runs", [(0, 1), (float("nan"), 1), (1, 0)])
def test_invalid_fuzz_budget_is_reported_before_execution(tmp_path, seconds, runs):
    report = GoalRunner().run(SHOP, BLOCK, source="t_state_shop.py", mode="fuzz", out=tmp_path,
                             fuzz_seconds=seconds, fuzz_runs=runs)
    assert report.summary["failed_stage"] == "validation"
