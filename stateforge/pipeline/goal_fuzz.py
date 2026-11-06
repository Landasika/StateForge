"""Publish a verified goal seed and require evidence from the actual fuzz engine."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys

from stateforge.solving.goal_checkpoint import GoalCheckpoint, inject_checkpoint_seed
from stateforge.fuzzing.corpus_manager import CorpusManager


def fuzz_checkpoint(checkpoint_path: str | Path, ir_path: str | Path,
                    work: str | Path, *, seconds: float, max_runs: int,
                    run_id: str, interpreter: str | None = None,
                    target_path: str | Path | None = None,
                    extra_args: list[str] | None = None) -> dict:
    if not math.isfinite(seconds) or seconds < 1 or max_runs < 1:
        raise ValueError("fuzzing requires seconds >= 1 and max_runs >= 1")
    checkpoint = GoalCheckpoint.load(checkpoint_path)
    if not (checkpoint.verification.get("verified")
            and checkpoint.replay_evidence.get("verified")):
        raise ValueError("fuzzing requires an executed and cold-replayed checkpoint")
    work = Path(work).resolve()
    corpus = work / "corpus"
    seed_id = inject_checkpoint_seed(checkpoint, corpus)
    seed_id = seed_id or CorpusManager.content_id(checkpoint.sequence_seed())
    digest = hashlib.sha256(checkpoint.sequence_seed()).hexdigest()
    out = work / "fuzz"
    out.mkdir(parents=True, exist_ok=True)
    receipt_path = out / "input_receipts.jsonl"
    receipt_path.unlink(missing_ok=True)
    log_path = out / "atheris.log"
    # A real application runs under its own interpreter and through its
    # in-process adapter, not through the analyzed source file itself.
    engine = interpreter or sys.executable
    target = str(target_path) if target_path is not None else checkpoint.source_file
    command = [engine, "-m", "stateforge.fuzzing.entry_driver",
               target, str(Path(ir_path).resolve()), str(corpus), str(out),
               "--seconds", str(seconds), "--max-runs", str(max_runs),
               "--mode", "feedback", "--lifecycle", "reset", "--seed", "1",
               "--run-id", run_id, "--target-block", checkpoint.target_block,
               "--input-receipts", str(receipt_path), "--receipt-sha256", digest]
    command += list(extra_args or [])
    result = {"verified": False, "consumed": False, "target_covered": False,
              "engine": "atheris", "seed_id": seed_id, "seed_sha256": digest,
              "sequence_steps": len(checkpoint.steps), "command": command,
              "log": str(log_path), "receipts": str(receipt_path)}
    with log_path.open("w", encoding="utf-8") as log:
        try:
            run = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                 timeout=max(120, seconds + 60))
            result["returncode"] = run.returncode
        except subprocess.TimeoutExpired:
            result["error"] = "Atheris subprocess exceeded its wall time budget"
        except OSError as exc:
            result["error"] = f"unable to start Atheris: {exc}"
    receipts = []
    if receipt_path.exists():
        for line in receipt_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                receipt = json.loads(line)
                if not isinstance(receipt, dict):
                    raise ValueError("receipt must be a JSON object")
                receipts.append(receipt)
            except (ValueError, TypeError) as exc:
                result["error"] = f"invalid Atheris input receipt: {exc}"
    matches = [r for r in receipts if r.get("sha256") == digest and r.get("run_id") == run_id]
    result["matching_receipts"] = matches
    result["consumed"] = bool(matches)
    result["target_covered"] = any(r.get("target_covered") for r in matches)
    result["verified"] = bool("error" not in result and result.get("returncode") == 0 and any(
        r.get("engine") == "atheris" and r.get("execution_recorded")
        and r.get("phase") == "input_callback" and r.get("error") is None
        and r.get("lifecycle") == "reset" and r.get("isolation") == "reset"
        and r.get("completed_steps") == len(checkpoint.steps)
        and r.get("target_block") == checkpoint.target_block and r.get("target_covered")
        for r in matches))
    if not result["verified"] and "error" not in result:
        result["error"] = "Atheris did not confirm isolated execution of the exact generated seed; see log and receipts"
    (out / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
