"""Cold-process replay entry point; no planning or LLM calls occur here."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys

from stateforge.core.ir import ProgramIR
from stateforge.runtime.plan_executor import verify_plan_execution
from stateforge.solving.goal_checkpoint import GoalCheckpoint, replay_checkpoint
from stateforge.state.extractor import extract_state_facts
from stateforge.state.goal_to_plan import dispatch_arms


def cold_replay(checkpoint: str | Path, ir_path: str | Path,
                out: str | Path, *, timeout: float = 30) -> dict:
    """Start a fresh interpreter, and require both its exit status and evidence."""
    output = Path(out).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    command = [sys.executable, "-m", "stateforge.solving.checkpoint_replay",
               str(Path(checkpoint).resolve()), str(Path(ir_path).resolve()), str(output)]
    try:
        run = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        evidence = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {
            "verified": False, "error": "replay process produced no evidence"}
        evidence.update(returncode=run.returncode, stderr=run.stderr[-4000:])
        evidence["verified"] = bool(
            run.returncode == 0 and evidence.get("verified") and evidence.get("cold_start")
            and isinstance(evidence.get("pid"), int) and evidence["pid"] > 0
            and evidence["pid"] != os.getpid() and evidence.get("parent_pid") == os.getpid())
    except subprocess.TimeoutExpired:
        evidence = {"verified": False, "error": f"cold replay timed out after {timeout}s"}
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkpoint")
    parser.add_argument("ir")
    parser.add_argument("out")
    args = parser.parse_args()
    evidence = {"verified": False, "pid": os.getpid(), "parent_pid": os.getppid(),
                "cold_start": True}
    try:
        checkpoint = GoalCheckpoint.load(args.checkpoint)
        ir = ProgramIR.from_json(args.ir)
        facts = extract_state_facts(Path(ir.metadata["root"]), ir=ir)
        execution = replay_checkpoint(checkpoint, ir, facts,
                                      dispatch_arms(checkpoint.source_file))
        verification = verify_plan_execution(execution, len(checkpoint.steps))
        evidence.update(verified=verification["verified"], execution=asdict(execution),
                        verification=verification, checkpoint_id=checkpoint.checkpoint_id,
                        target_block=checkpoint.target_block, source_hash=checkpoint.source_hash)
    except Exception as exc:
        evidence["error"] = f"{type(exc).__name__}: {exc}"
    Path(args.out).write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if evidence["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
