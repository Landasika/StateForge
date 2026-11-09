"""Cold-process replay of an HTTP (real application) goal checkpoint.

The micro-target replay path executes ``target({"json": ...})``; a real
application is executed through its own interpreter and test client, so it
needs a separate entry point that still produces the same kind of evidence:
a fresh process, a fresh unauthenticated client, and traced target coverage.

Two callers share this module:

- ``--mode chain`` replays the whole checkpoint and reports whether the
  target block is still covered from a cold start;
- ``--mode final-only`` executes just the last request, which is how a
  runner proves that the prefix is necessary (unauthenticated access does
  not reach the target) and that the reset boundary is effective.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys

from stateforge.core.ir import ProgramIR
from stateforge.runtime.http_plan_executor import execute_app_chain, verify_http_execution
from stateforge.solving.goal_checkpoint import GoalCheckpoint


def _execute(checkpoint: GoalCheckpoint, ir: ProgramIR, harness: str,
             app_root: str, trace_root: str | None, credentials: dict | None,
             *, mode: str) -> dict:
    import importlib

    # Validate before importing the harness: importing it can bootstrap/mutate
    # the application, and must not run for a stale or mismatched checkpoint.
    from stateforge.analysis.static.icfg import _source_hash

    if checkpoint.transport != "http":
        raise ValueError("checkpoint is not an HTTP checkpoint")
    if not ir.metadata.get("root"):
        raise ValueError("checkpoint IR has no analysis root")
    root = Path(ir.metadata["root"]).resolve()
    block = ir.blocks.get(checkpoint.target_block)
    source = Path(checkpoint.source_file)
    source = (source if source.is_absolute() else root / source).resolve()
    if block is None or (root / block.file).resolve() != source:
        raise ValueError("checkpoint target/source does not match the analyzed block")
    if not source.is_file() or not source.is_relative_to(root):
        raise ValueError("checkpoint source must exist inside the analysis root")
    if (not checkpoint.source_hash
            or checkpoint.source_hash != ir.metadata.get("source_hash")
            or checkpoint.source_hash != _source_hash(root)):
        raise ValueError("checkpoint source hash mismatch; analyze and verify again")

    module = importlib.import_module(harness)
    factory = getattr(module, "build_client", None)
    if not callable(factory):
        raise ValueError(
            f"harness {harness} must expose build_client(authenticated=False)")
    # A fresh client is the point of a cold replay: no inherited session.
    client = factory(authenticated=False)
    segment = checkpoint.to_segment()
    if mode == "final-only":
        segment.steps = segment.steps[-1:]
    execution = execute_app_chain(
        segment, client, ir, checkpoint.target_block, app_root,
        credentials=credentials, trace_root=trace_root or app_root,
        auth_probe=getattr(module, "is_authenticated", None))
    verification = verify_http_execution(execution, len(segment.steps))
    return {"execution": asdict(execution), "verification": verification,
            "steps_executed": len(segment.steps)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkpoint")
    parser.add_argument("ir")
    parser.add_argument("out")
    parser.add_argument("--harness", required=True,
                        help="harness module exposing build_client/is_authenticated")
    parser.add_argument("--app-root", required=True)
    parser.add_argument("--trace-root", default=None)
    parser.add_argument("--mode", choices=("chain", "final-only"), default="chain")
    parser.add_argument("--user", default="probe")
    parser.add_argument("--password", default="Passw0rd!")
    args = parser.parse_args(argv)
    evidence = {"verified": False, "pid": os.getpid(), "parent_pid": os.getppid(),
                "cold_start": True, "mode": args.mode}
    try:
        checkpoint = GoalCheckpoint.load(args.checkpoint)
        if checkpoint.transport != "http":
            raise ValueError("checkpoint is not an HTTP checkpoint")
        ir = ProgramIR.from_json(args.ir)
        evidence.update(
            checkpoint_id=checkpoint.checkpoint_id,
            target_block=checkpoint.target_block,
            source_hash=checkpoint.source_hash,
            **_execute(checkpoint, ir, args.harness, args.app_root,
                       args.trace_root,
                       {"username": args.user, "password": args.password},
                       mode=args.mode))
        evidence["verified"] = bool(evidence["verification"]["verified"])
    except Exception as exc:                          # noqa: BLE001
        evidence["error"] = f"{type(exc).__name__}: {exc}"
    Path(args.out).write_text(json.dumps(evidence, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    return 0 if evidence["verified"] else 1


def cold_replay_http(checkpoint: str | Path, ir_path: str | Path,
                     out: str | Path, *, python: str | None = None,
                     harness: str, app_root: str | Path,
                     trace_root: str | Path | None = None,
                     credentials: dict | None = None,
                     mode: str = "chain", timeout: float = 300) -> dict:
    """Run the replay in an independent interpreter and require its evidence."""
    output = Path(out).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    command = [python or sys.executable, "-m",
               "stateforge.solving.http_checkpoint_replay",
               str(Path(checkpoint).resolve()), str(Path(ir_path).resolve()),
               str(output), "--harness", harness,
               "--app-root", str(Path(app_root).resolve()),
               "--mode", mode]
    if trace_root is not None:
        command += ["--trace-root", str(Path(trace_root).resolve())]
    if credentials:
        command += ["--user", str(credentials["username"]),
                    "--password", str(credentials["password"])]
    try:
        run = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        evidence = (json.loads(output.read_text(encoding="utf-8"))
                    if output.exists() else
                    {"verified": False, "error": "replay process produced no evidence"})
        evidence.update(returncode=run.returncode, command=command,
                        stderr=run.stderr[-4000:])
        evidence["verified"] = bool(
            run.returncode == 0 and evidence.get("verified")
            and evidence.get("cold_start")
            and isinstance(evidence.get("pid"), int) and evidence["pid"] > 0
            and evidence["pid"] != os.getpid()
            and evidence.get("parent_pid") == os.getpid())
    except subprocess.TimeoutExpired:
        evidence = {"verified": False, "command": command,
                    "error": f"cold replay timed out after {timeout}s"}
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    return evidence


if __name__ == "__main__":
    raise SystemExit(main())
