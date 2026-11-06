"""GoalRunner contracts: generated chains, execution evidence, stage failures."""

import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

from stateforge.core.model import PlanSegment, PlanStep
from stateforge.pipeline import GoalRunner

SHOP = Path(__file__).resolve().parents[2] / "targets" / "stateful"
BLOCK = "t_state_shop.py::_handle::91-92::block::1"
SOURCE = "t_state_shop.py"


def test_runner_executes_automatically_derived_chain(tmp_path):
    report = GoalRunner().run(SHOP, BLOCK, source=SOURCE, out=tmp_path)
    assert report.success, report.summary
    assert report.summary["verified"] is True
    assert [s["json"]["op"] for s in report.plan["steps"]] == [
        "register", "create_order", "ship_order"]
    assert "ship_privileged" in report.execution["hits"]
    assert all(s["ok"] for s in report.execution["steps"])
    assert {p.name for p in tmp_path.iterdir() if p.is_file()} == {
        "ir.json", "goal.json", "plan.json", "execution.json", "summary.json"}
    assert json.loads((tmp_path / "summary.json").read_text())["verified"]


def test_plan_mode_does_not_execute_or_claim_verification(tmp_path):
    with patch("stateforge.pipeline.goal_runner.load_target_module") as load:
        report = GoalRunner().run(SHOP, BLOCK, source=SOURCE, mode="plan", out=tmp_path)
    load.assert_not_called()
    assert report.success and report.summary["status"] == "planned"
    assert not report.summary["verified"] and not report.summary["target_covered"]
    assert json.loads((tmp_path / "execution.json").read_text())["status"] == "not_run"


def test_unknown_block_reports_analysis_failure_and_replaces_stale_success(tmp_path):
    for name in ("ir", "goal", "plan", "execution", "summary"):
        (tmp_path / f"{name}.json").write_text('{"verified":true}')
    report = GoalRunner().run(SHOP, "missing-block", source=SOURCE, out=tmp_path)
    assert not report.success
    assert report.summary["failed_stage"] == "analysis"
    assert "unknown target block" in report.summary["error"]["message"]
    assert json.loads((tmp_path / "plan.json").read_text())["status"] == "not_run"


def test_source_must_match_target_block(tmp_path):
    report = GoalRunner().run(SHOP, BLOCK, source="t_state_gate.py", out=tmp_path)
    assert not report.success
    assert report.summary["failed_stage"] == "analysis"
    assert "does not belong" in report.summary["error"]["message"]


def test_failed_planning_is_not_an_execution_failure(tmp_path):
    with patch("stateforge.pipeline.goal_runner.plan_for_block", return_value=None):
        report = GoalRunner().run(SHOP, BLOCK, source=SOURCE, out=tmp_path)
    assert report.summary["failed_stage"] == "planning"
    assert report.goal is not None and report.execution is None


def test_bad_plan_keeps_blocking_evidence(tmp_path):
    from stateforge.state.goal_to_plan import plan_for_block

    def broken(ir, facts, block):
        goal, _ = plan_for_block(ir, facts, block)
        return goal, PlanSegment(goal="broken", steps=[
            PlanStep(action="POST op=ship_order", json={"op": "ship_order"})])

    with patch("stateforge.pipeline.goal_runner.plan_for_block", side_effect=broken):
        report = GoalRunner().run(SHOP, BLOCK, source=SOURCE, out=tmp_path)
    assert report.summary["failed_stage"] == "verification"
    assert not report.summary["verified"]
    assert report.execution["steps"][0]["blocking"]


def test_goal_run_cli(tmp_path):
    result = subprocess.run([
        sys.executable, "-m", "stateforge.cli", "goal-run", str(SHOP),
        "--source", SOURCE, "--block", BLOCK, "--mode", "execute", "--out", str(tmp_path)
    ], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
    assert json.loads((tmp_path / "summary.json").read_text())["verified"] is True


def test_cli_unknown_block_nonzero_with_report(tmp_path):
    result = subprocess.run([
        sys.executable, "-m", "stateforge.cli", "goal-run", str(SHOP),
        "--source", SOURCE, "--block", "missing", "--out", str(tmp_path)
    ], capture_output=True, text=True, timeout=30)
    assert result.returncode == 2
    assert json.loads((tmp_path / "summary.json").read_text())["failed_stage"] == "analysis"


def test_checkpoint_requires_cold_process_replay_and_replays_without_planning(tmp_path):
    from stateforge.solving.goal_checkpoint import GoalCheckpoint

    original = GoalRunner().run(SHOP, BLOCK, source=SOURCE, out=tmp_path / "original")
    assert original.success, original.summary
    checkpoint = GoalCheckpoint.load(original.summary["checkpoint"])
    assert checkpoint.source_hash
    assert checkpoint.replay_evidence["verified"] is True
    assert checkpoint.replay_evidence["pid"] != os.getpid()
    assert checkpoint.replay_evidence["parent_pid"] == os.getpid()
    with patch("stateforge.pipeline.goal_runner.plan_for_block") as plan, patch(
        "stateforge.pipeline.goal_runner.derive_state_goal"
    ) as derive:
        replay = GoalRunner().run(SHOP, BLOCK, source=SOURCE, mode="replay",
                                 checkpoint=original.summary["checkpoint"], out=tmp_path / "replay")
    plan.assert_not_called()
    derive.assert_not_called()
    assert replay.success, replay.summary
    assert replay.summary["cold_replay"]["verified"]
    assert "ship_privileged" in replay.execution["hits"]


def test_failed_cold_replay_does_not_promote_checkpoint(tmp_path):
    with patch("stateforge.pipeline.goal_runner.cold_replay", return_value={
        "verified": False, "error": "target not covered after cold start"
    }):
        report = GoalRunner().run(SHOP, BLOCK, source=SOURCE, out=tmp_path)
    assert not report.success and report.summary["failed_stage"] == "cold_replay"
    assert not list(tmp_path.rglob("checkpoints/*.json"))


def test_broken_checkpoint_fails_with_execution_evidence(tmp_path):
    original = GoalRunner().run(SHOP, BLOCK, source=SOURCE, out=tmp_path / "original")
    path = Path(original.summary["checkpoint"])
    checkpoint = json.loads(path.read_text())
    checkpoint["steps"] = [{"json": {"op": "ship_order"}}]
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps(checkpoint))
    report = GoalRunner().run(SHOP, BLOCK, source=SOURCE, mode="replay",
                             checkpoint=broken, out=tmp_path / "replay")
    assert report.summary["failed_stage"] == "cold_replay"
    assert not report.summary["verified"]
    assert report.execution["steps"][0]["blocking"]


def test_stale_checkpoint_rejected_before_execution(tmp_path):
    original = GoalRunner().run(SHOP, BLOCK, source=SOURCE, out=tmp_path / "original")
    checkpoint = json.loads(Path(original.summary["checkpoint"]).read_text())
    checkpoint["source_hash"] = "wrong-source-version"
    stale = tmp_path / "stale.json"
    stale.write_text(json.dumps(checkpoint))
    report = GoalRunner().run(SHOP, BLOCK, source=SOURCE, mode="replay",
                             checkpoint=stale, out=tmp_path / "replay")
    assert not report.success
    assert "hash mismatch" in report.summary["error"]["message"]


def test_nested_source_uses_analysis_root_for_trace_mapping(tmp_path):
    root = tmp_path / "site-packages" / "application"
    nested = root / "nested"
    nested.mkdir(parents=True)
    shutil.copy2(SHOP / SOURCE, nested / SOURCE)
    report = GoalRunner().run(root, "nested/" + BLOCK, source="nested/" + SOURCE,
                             out=tmp_path / "out")
    assert report.success, report.summary
    assert report.summary["cold_replay"]["verified"]


@pytest.mark.parametrize("function,stage", [
    ("extract_state_facts", "state_extraction"),
    ("derive_state_goal", "goal_derivation"),
])
def test_analysis_failures_report_the_responsible_stage(tmp_path, function, stage):
    with patch(f"stateforge.pipeline.goal_runner.{function}", side_effect=ValueError("bad input")):
        report = GoalRunner().run(SHOP, BLOCK, source=SOURCE, out=tmp_path)
    assert report.summary["failed_stage"] == stage
    assert report.summary["error"]["message"] == "bad input"
    assert report.execution is None


def test_exception_after_target_hit_is_an_execution_failure(tmp_path):
    from stateforge.runtime.plan_executor import load_target_module

    module = load_target_module(SHOP / SOURCE)
    original_target = module.target

    def raises_after_request(data):
        original_target(data)
        raise ValueError("request failed after its side effects")

    module.target = raises_after_request
    with patch("stateforge.pipeline.goal_runner.load_target_module", return_value=module):
        report = GoalRunner().run(SHOP, BLOCK, source=SOURCE, out=tmp_path)
    assert report.summary["target_covered"] is True
    assert report.summary["failed_stage"] == "execution"
    assert not report.summary["verified"]
    assert "exception:ValueError" in report.execution["steps"][-1]["blocking"]
    assert not list(tmp_path.rglob("checkpoints/*.json"))
