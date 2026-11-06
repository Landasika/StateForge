"""State-goal pipeline for a real web application.

This is the same staged pipeline as :mod:`stateforge.pipeline.goal_runner`
(analysis → state facts → goal → plan → execute → verify → checkpoint →
cold replay → fuzz), with two differences that come from the target being a
running Django/Flask application rather than an in-process micro target:

1. planning goes through the application bridge
   (:mod:`stateforge.state.goal_app_bridge`), which turns a framework guard
   into an HTTP chain;
2. execution goes through the application's own interpreter and test client
   (:mod:`stateforge.runtime.http_plan_executor`), and the "cold start" that
   must reproduce the chain is a fresh process with a fresh unauthenticated
   client.

Nothing here re-derives goals or re-implements planning; it wires the
existing modules and refuses to report success without execution evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4

from stateforge.analysis.static.icfg import build_icfg
from stateforge.pipeline.goal_fuzz import fuzz_checkpoint
from stateforge.runtime.http_plan_executor import (execute_app_chain,
                                                   verify_http_execution)
from stateforge.solving.goal_checkpoint import (GoalCheckpoint,
                                                http_checkpoint_from_execution)
from stateforge.solving.http_checkpoint_replay import cold_replay_http
from stateforge.state.extractor import extract_state_facts
from stateforge.state.goal_app_bridge import (app_goal_constraints,
                                              django_auth_completion,
                                              plan_app_chain)

DEFAULT_USER = "probe"
DEFAULT_PASSWORD = "Passw0rd!"


@dataclass
class AppGoalRunReport:
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
                payload = {"status": "not_run",
                           "failed_stage": self.summary.get("failed_stage")}
            path = directory / f"{name}.json"
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)


class AppGoalRunner:
    """Analyze → plan → execute → verify → checkpoint → replay → fuzz a real app."""

    def __init__(self, *, python: str | None = None,
                 user: str = DEFAULT_USER, password: str = DEFAULT_PASSWORD):
        self.python = python
        self.user = user
        self.password = password

    def run(self, app_root: str | Path, block: str, *, framework: str = "django",
            analysis_root: str | Path | None = None,
            harness: str | None = None,
            adapter: str | Path | None = None,
            trace_root: str | Path | None = None,
            mode: str = "execute", out: str | Path | None = None,
            checkpoint: str | Path | None = None,
            fuzz_seconds: float = 2, fuzz_runs: int = 50) -> AppGoalRunReport:
        report = AppGoalRunReport(summary={
            "transport": "http", "run_id": uuid4().hex,
            "target": str(Path(app_root).resolve()), "target_block": block,
            "framework": framework, "mode": mode, "status": "running",
            "failed_stage": None, "stages": {}, "target_covered": False,
            "execution_verified": False, "verified": False,
            # Stable keys, so consumers never need a KeyError branch.
            "checkpoint": None, "prefix_necessary": None, "error": None,
        })
        if out is not None:
            report.save(out)
        root = Path(app_root).resolve()
        # Application modules (views, ORM) live where the trace can see them;
        # the analyzed root may be a wider tree the block id refers to.
        scope = Path(analysis_root).resolve() if analysis_root is not None else root
        trace = Path(trace_root).resolve() if trace_root is not None else scope
        stage = "validation"
        try:
            if mode not in ("plan", "execute", "replay", "fuzz"):
                raise ValueError(f"unsupported mode: {mode}")
            if (mode == "replay") != (checkpoint is not None):
                raise ValueError("--checkpoint is required only with --mode replay")
            if not (mode == "replay") and not harness:
                raise ValueError("--harness is required to execute a real application")
            if mode == "fuzz" and (out is None or not math.isfinite(fuzz_seconds)
                                   or fuzz_seconds < 1 or fuzz_runs < 1):
                raise ValueError("fuzz mode requires --out, --fuzz-seconds >= 1, --fuzz-runs >= 1")
            if not root.is_dir():
                raise ValueError(f"application root must be a directory: {root}")
            if not scope.is_dir() or not str(scope).startswith(str(root)):
                raise ValueError(f"analysis root must be inside the application: {scope}")
            report.summary["stages"][stage] = "ok"

            stage = "analysis"
            ir = build_icfg(scope)
            report.ir = ir.to_dict()
            if block not in ir.blocks:
                raise ValueError(f"unknown target block: {block} (analysis root {scope})")
            report.summary["source"] = str((scope / ir.blocks[block].file).resolve())
            report.summary["analysis_root"] = str(scope)
            report.summary["stages"][stage] = "ok"

            stage = "state_extraction"
            facts = extract_state_facts(scope, ir=ir)
            report.summary["state_fact_count"] = len(facts)
            report.summary["stages"][stage] = "ok"

            if mode == "replay":
                stage = "checkpoint_loading"
                saved = GoalCheckpoint.load(checkpoint)
                if saved.transport != "http":
                    raise ValueError("checkpoint was not produced by an application run")
                if saved.target_block != block:
                    raise ValueError("checkpoint does not match requested target block")
                report.goal = {"target_block": block,
                               "constraints": saved.goal_constraints,
                               "origin": "checkpoint"}
                report.plan = saved.to_segment().model_dump(mode="json")
                report.summary["stages"][stage] = "ok"
                stage = "cold_replay"
                evidence, replayed = self._cold_replay(
                    saved, ir, report, out, harness, root, trace, mode="chain")
                report.execution = evidence.get("execution")
                report.summary["verification"] = evidence.get("verification", {})
                report.summary["target_covered"] = bool(
                    report.execution and report.execution["target_covered"])
                if not evidence["verified"]:
                    raise ValueError(evidence.get("error") or
                                     "cold replay did not verify the target")
                report.summary.update(verified=True, execution_verified=True,
                                      status="verified",
                                      input_checkpoint=str(Path(checkpoint).resolve()),
                                      checkpoint=replayed)
                report.summary["stages"][stage] = "ok"
                return report

            stage = "goal_derivation"
            model = self._analyze(root, framework)
            constraints = app_goal_constraints(model, ir, block)
            if not constraints:
                raise ValueError(
                    "no state constraint could be derived for the target block")
            report.goal = {"target_block": block,
                           "constraints": [{"var": p.var, "op": p.op,
                                            "value": p.value} for p in constraints]}
            report.summary["stages"][stage] = "ok"

            stage = "planning"
            constraints, segment = plan_app_chain(model, ir, block)
            if segment is None or not segment.steps:
                raise ValueError("planning produced no request chain")
            report.plan = segment.model_dump(mode="json")
            report.summary["stages"][stage] = "ok"
            report.summary["step_count"] = len(segment.steps)
            if mode == "plan":
                report.summary["status"] = "planned"
                return report

            stage = "execution"
            execution, client_note = self._execute(
                segment, ir, block, root, trace, harness, authenticated=False)
            report.execution = execution
            report.summary["target_covered"] = execution["target_covered"]
            report.summary["stages"][stage] = "ok"
            if client_note:
                report.summary["notes"] = [client_note]

            stage = "verification"
            verification = verify_http_execution(
                _wrap(execution), len(segment.steps))
            report.summary["verification"] = verification
            if not verification["verified"]:
                raise ValueError(
                    "execution did not verify the complete chain and target block: "
                    + "; ".join(verification["blocking"] or verification["notes"]))
            report.summary["execution_verified"] = True

            stage = "isolation"
            # The prefix must be load-bearing: the last request alone, from a
            # fresh unauthenticated client, must not reach the target.
            isolated = self._check_prefix_necessary(
                execution, ir, block, root, trace, harness)
            report.summary["prefix_necessary"] = isolated
            if not isolated:
                raise ValueError(
                    "target block was reached without the planned prefix; the chain "
                    "is not evidence of state construction")
            report.summary["stages"][stage] = "ok"

            stage = "checkpoint"
            candidate = http_checkpoint_from_execution(
                block, str((scope / ir.blocks[block].file).resolve()),
                report.goal["constraints"], _wrap(execution), ir=ir,
                fuzz_target=str(Path(adapter).resolve()) if adapter else "")
            if candidate is None:
                raise ValueError("execution evidence is insufficient for a checkpoint")
            stage = "cold_replay"
            evidence, checkpoint_path = self._cold_replay(
                candidate, ir, report, out, harness, root, trace, mode="chain")
            if not evidence["verified"]:
                raise ValueError(evidence.get("error") or
                                 "cold replay did not verify the target")
            report.summary.update(status="verified", checkpoint=checkpoint_path)
            report.summary["stages"][stage] = "ok"
            report.summary["stages"]["checkpoint"] = "ok"

            if mode == "fuzz":
                stage = "fuzzing"
                if not adapter:
                    raise ValueError("fuzz mode requires --adapter (in-process target file)")
                work = Path(checkpoint_path).parent.parent
                fuzzing = fuzz_checkpoint(
                    checkpoint_path, work / "ir.json", work,
                    seconds=fuzz_seconds, max_runs=fuzz_runs,
                    run_id=report.summary["run_id"],
                    interpreter=self._interpreter(),
                    target_path=Path(adapter).resolve())
                report.summary["fuzzing"] = fuzzing
                if not fuzzing["verified"]:
                    raise ValueError(fuzzing.get("error") or
                                     "Atheris did not confirm seed consumption")
                report.summary["stages"][stage] = "ok"
            report.summary["verified"] = True
        except Exception as exc:                       # noqa: BLE001
            report.summary.update(status="failed", failed_stage=stage,
                                  error={"type": type(exc).__name__,
                                         "message": str(exc)})
            report.summary["stages"][stage] = "failed"
        finally:
            if out is not None:
                report.save(out)
        return report

    # ---------------------------------------------------------------- helpers

    def _interpreter(self) -> str:
        """The application's own interpreter, which owns its dependencies."""
        return self.python or os.environ.get("SF_APP_PYTHON") or sys.executable

    def _analyze(self, root: Path, framework: str):
        """Analyze the application, including framework-level completions."""
        from stateforge.analysis.analyzer import analyze
        model = analyze(root, framework)
        if framework == "django":
            # A Django app's @login_required guard and the built-in LoginView
            # are structural facts of the framework, not app-specific rules.
            model = django_auth_completion(model, root)
        return model

    def _execute(self, segment, ir, block, root: Path, trace: Path,
                 harness: str, *, authenticated: bool):
        """Run one chain in this process; returns (evidence dict, note)."""
        import importlib

        module = importlib.import_module(harness)
        factory = getattr(module, "build_client", None)
        if not callable(factory):
            raise ValueError(
                f"harness {harness} must expose build_client(authenticated=False)")
        client = factory(authenticated=authenticated)
        execution = execute_app_chain(
            segment, client, ir, block, root,
            credentials={"username": self.user, "password": self.password},
            trace_root=trace,
            auth_probe=getattr(module, "is_authenticated", None))
        return _dump(execution)

    def _check_prefix_necessary(self, execution, ir, block, root: Path,
                                trace: Path, harness: str) -> bool:
        """True when the final request alone does not reach the target block."""
        steps = execution["steps"]
        if len(steps) < 2:
            return True
        from stateforge.core.model import PlanSegment, PlanStep

        last = steps[-1].get("request") or {"method": steps[-1]["method"],
                                            "path": steps[-1]["path"]}
        isolated_segment = PlanSegment(goal="isolation-probe", steps=[
            PlanStep(action=f"{last['method']} {last['path']}",
                     json=dict(last.get("json", {})),
                     form=dict(last.get("form", {})),
                     headers=dict(last.get("header", {})))])
        probe, _note = self._execute(
            isolated_segment, ir, block, root, trace, harness, authenticated=False)
        return not probe["target_covered"]

    def _cold_replay(self, candidate: GoalCheckpoint, ir, report, out,
                     harness: str, root: Path, trace: Path, *, mode: str):
        """Replay in an independent interpreter; promote only on success."""
        with TemporaryDirectory(prefix="stateforge-app-checkpoint-") as temporary:
            work = (Path(out) / "runs" / report.summary["run_id"]
                    if out is not None else Path(temporary))
            work.mkdir(parents=True, exist_ok=True)
            ir_path = work / "ir.json"
            ir.to_json(ir_path)
            candidate_path = Path(temporary) / "candidate.json"
            candidate_path.write_text(candidate.to_json(), encoding="utf-8")
            evidence = cold_replay_http(
                candidate_path, ir_path, work / "cold_replay.json",
                python=self._interpreter(), harness=harness, app_root=root,
                trace_root=trace,
                credentials={"username": self.user, "password": self.password},
                mode=mode)
            report.summary["cold_replay"] = {
                k: evidence[k] for k in ("verified", "pid", "parent_pid",
                                         "error", "returncode", "mode")
                if k in evidence}
            if out is not None:
                report.summary["cold_replay"]["path"] = str(
                    (work / "cold_replay.json").resolve())
            path = None
            if evidence["verified"]:
                candidate.verification = evidence["verification"]
                candidate.replay_evidence = dict(
                    report.summary["cold_replay"], cold_start=True)
                if out is not None:
                    path = str(candidate.save(work / "checkpoints").resolve())
            return evidence, path


def _dump(execution) -> tuple[dict, str | None]:
    """Saved evidence: dataclasses convert directly, plain objects by fields."""
    from dataclasses import asdict, is_dataclass
    if is_dataclass(execution) and not isinstance(execution, type):
        return asdict(execution), None
    steps = []
    for step in execution.steps:
        if is_dataclass(step) and not isinstance(step, type):
            steps.append(asdict(step))
        else:
            steps.append({k: v for k, v in vars(step).items()
                          if not k.startswith("_")})
    return ({"steps": steps,
             "target_covered": execution.target_covered,
             "verified": execution.verified,
             "notes": list(getattr(execution, "notes", []))}, None)


def _wrap(execution: dict):
    """Rebuild the dataclass the verifier expects from saved evidence."""
    from types import SimpleNamespace
    return SimpleNamespace(
        steps=[SimpleNamespace(**s) for s in execution["steps"]],
        target_covered=execution["target_covered"],
        verified=execution.get("verified", False),
        notes=list(execution.get("notes", [])))
