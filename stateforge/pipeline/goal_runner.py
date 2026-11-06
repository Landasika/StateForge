"""One auditable entry point for static state goals and traced execution.

Planning remains in state.goal_to_plan; this module owns orchestration and
artifacts only. A generated plan is never reported as an executed success.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from stateforge.analysis.static.icfg import build_icfg
from stateforge.runtime.plan_executor import execute_plan, load_target_module, verify_plan_execution
from stateforge.solving.checkpoint_replay import cold_replay
from stateforge.solving.goal_checkpoint import GoalCheckpoint, checkpoint_from_execution
from stateforge.pipeline.goal_fuzz import fuzz_checkpoint
from stateforge.state.extractor import extract_state_facts
from stateforge.state.goal_derivation import derive_state_goal
from stateforge.state.goal_to_plan import dispatch_arms, plan_for_block


@dataclass
class GoalRunReport:
    ir: dict | None = None
    goal: dict | None = None
    plan: dict | None = None
    execution: dict | None = None
    summary: dict = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.summary.get("status") in ("planned", "verified")

    def save(self, out: str | Path) -> None:
        directory = Path(out)
        directory.mkdir(parents=True, exist_ok=True)
        for name in ("ir", "goal", "plan", "execution", "summary"):
            payload = getattr(self, name)
            if payload is None:
                payload = {"status": "not_run", "failed_stage": self.summary.get("failed_stage")}
            path = directory / f"{name}.json"
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            temporary.replace(path)


class GoalRunner:
    """Coordinate existing algorithms for a C10 micro target with target(data)."""

    def run(self, target: str | Path, block: str, *, source: str | Path,
            mode: str = "execute", out: str | Path | None = None,
            checkpoint: str | Path | None = None,
            fuzz_seconds: float = 2, fuzz_runs: int = 100) -> GoalRunReport:
        report = GoalRunReport(summary={
            "run_id": uuid4().hex, "target": str(Path(target).resolve()),
            "target_block": block, "source": str(source), "mode": mode,
            "status": "running", "failed_stage": None, "stages": {},
            "target_covered": False, "execution_verified": False, "verified": False,
        })
        # Clear the five fixed manifests before starting; no previous success
        # survives a failed run that reuses an output directory.
        if out is not None:
            report.save(out)
        stage = "validation"
        try:
            root = Path(target).resolve()
            source_file = (root / source).resolve()
            if mode not in ("plan", "execute", "replay", "fuzz"):
                raise ValueError(f"unsupported mode: {mode}")
            if (mode == "replay") != (checkpoint is not None):
                raise ValueError("--checkpoint is required only with --mode replay")
            if mode == "fuzz" and (out is None or not math.isfinite(fuzz_seconds)
                                   or fuzz_seconds < 1 or fuzz_runs < 1):
                raise ValueError("fuzz mode requires --out, --fuzz-seconds >= 1 and --fuzz-runs >= 1")
            if not root.is_dir():
                raise ValueError(f"target must be a source directory: {root}")
            if not source_file.is_relative_to(root) or not source_file.is_file():
                raise ValueError(f"source must be a file inside target: {source_file}")
            report.summary["source"] = str(source_file)
            report.summary["stages"][stage] = "ok"

            stage = "analysis"
            ir = build_icfg(root)
            report.ir = ir.to_dict()
            if block not in ir.blocks:
                raise ValueError(f"unknown target block: {block}")
            if (root / ir.blocks[block].file).resolve() != source_file:
                raise ValueError("target block does not belong to --source")
            report.summary["stages"][stage] = "ok"

            stage = "state_extraction"
            facts = extract_state_facts(root, ir=ir)
            report.summary["state_fact_count"] = len(facts)
            report.summary["stages"][stage] = "ok"

            if mode == "replay":
                stage = "checkpoint_loading"
                saved = GoalCheckpoint.load(checkpoint)
                if (saved.target_block != block
                        or Path(saved.source_file).resolve() != source_file):
                    raise ValueError("checkpoint does not match requested source and block")
                report.goal = {"target_block": block, "constraints": saved.goal_constraints,
                               "origin": "checkpoint"}
                report.plan = saved.to_segment().model_dump(mode="json")
                report.summary["stages"][stage] = "ok"
                stage = "cold_replay"
                evidence, replayed_path = self._cold_replay(saved, ir, report, out)
                report.execution = evidence.get("execution")
                report.summary["verification"] = evidence.get("verification", {})
                report.summary["target_covered"] = bool(
                    report.execution and report.execution["target_covered"])
                if not evidence["verified"]:
                    raise ValueError(evidence.get("error", "cold replay did not verify the target"))
                report.summary.update(verified=True, execution_verified=True, status="verified",
                                      input_checkpoint=str(Path(checkpoint).resolve()),
                                      checkpoint=replayed_path)
                report.summary["stages"][stage] = "ok"
                return report

            stage = "goal_derivation"
            goal = derive_state_goal(ir, facts, block)
            if goal is None:
                raise ValueError("no state goal could be derived for target block")
            report.goal = goal.to_dict()
            report.summary["stages"][stage] = "ok"

            stage = "planning"
            planned = plan_for_block(ir, facts, block)
            if planned is None or not planned[1].steps:
                raise ValueError("planning produced no request chain")
            goal, segment = planned
            report.goal = goal.to_dict()
            report.plan = segment.model_dump(mode="json")
            report.summary["stages"][stage] = "ok"
            report.summary["step_count"] = len(segment.steps)
            if mode == "plan":
                report.summary["status"] = "planned"
                return report

            stage = "execution"
            module = load_target_module(source_file)
            execution = execute_plan(segment, module, ir, facts, block,
                                     str(source_file), dispatch_arms(source_file))
            report.execution = asdict(execution)
            report.summary["target_covered"] = execution.target_covered
            if not execution.steps:
                raise ValueError("; ".join(execution.notes) or "no requests executed")
            if any(s.blocking and s.blocking.startswith("exception:") for s in execution.steps):
                raise ValueError("target raised during execution; see per-step blocking evidence")
            report.summary["stages"][stage] = "ok"

            stage = "verification"
            verification = verify_plan_execution(execution, len(segment.steps))
            report.summary["verification"] = verification
            if not verification["verified"]:
                raise ValueError("execution did not verify the complete chain and target block")
            report.summary["execution_verified"] = True
            report.summary["stages"][stage] = "ok"

            stage = "checkpoint"
            candidate = checkpoint_from_execution(block, str(source_file), goal, execution, ir=ir)
            if candidate is None:
                raise ValueError("execution evidence is insufficient for a checkpoint")
            stage = "cold_replay"
            evidence, checkpoint_path = self._cold_replay(candidate, ir, report, out)
            if not evidence["verified"]:
                raise ValueError(evidence.get("error", "cold replay did not verify the target"))
            report.summary.update(status="verified", checkpoint=checkpoint_path)
            report.summary["stages"][stage] = "ok"
            report.summary["stages"]["checkpoint"] = "ok"
            if mode == "fuzz":
                stage = "fuzzing"
                work = Path(checkpoint_path).parent.parent
                fuzzing = fuzz_checkpoint(checkpoint_path, work / "ir.json", work,
                                          seconds=fuzz_seconds, max_runs=fuzz_runs,
                                          run_id=report.summary["run_id"])
                report.summary["fuzzing"] = fuzzing
                if not fuzzing["verified"]:
                    raise ValueError(fuzzing["error"])
                report.summary["stages"][stage] = "ok"
            report.summary["verified"] = True
        except Exception as exc:
            report.summary.update(status="failed", failed_stage=stage,
                                  error={"type": type(exc).__name__, "message": str(exc)})
            report.summary["stages"][stage] = "failed"
        finally:
            if out is not None:
                report.save(out)
        return report

    @staticmethod
    def _cold_replay(candidate, ir, report, out):
        # Candidates are transient; only a successful cold replay is promoted
        # into the persistent checkpoints directory. Per-run paths prevent old
        # replay/fuzz receipts from certifying a later run.
        with TemporaryDirectory(prefix="stateforge-checkpoint-") as temporary:
            work = (Path(out) / "runs" / report.summary["run_id"]
                    if out is not None else Path(temporary))
            work.mkdir(parents=True, exist_ok=True)
            ir_path = work / "ir.json"
            ir.to_json(ir_path)
            candidate_path = Path(temporary) / "candidate.json"
            candidate_path.write_text(candidate.to_json(), encoding="utf-8")
            evidence = cold_replay(candidate_path, ir_path, work / "cold_replay.json")
            report.summary["cold_replay"] = {
                k: evidence[k] for k in ("verified", "pid", "parent_pid", "error", "returncode")
                if k in evidence}
            if out is not None:
                report.summary["cold_replay"]["path"] = str((work / "cold_replay.json").resolve())
            path = None
            if evidence["verified"]:
                candidate.verification = evidence["verification"]
                candidate.replay_evidence = dict(report.summary["cold_replay"], cold_start=True)
                if out is not None:
                    path = str(candidate.save(work / "checkpoints").resolve())
            return evidence, path
