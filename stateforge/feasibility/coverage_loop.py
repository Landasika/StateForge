"""Phase-4.2-E:LLM-Guided Coverage Loop(stall → slice → LLM → validate →
execute → coverage delta → corpus admission)。

复用既有七件(ICFG/observer/mapper/slice/validator/mutator/corpus),
新增:per-frontier K 次预算、失败历史喂入下一轮 prompt、全局预算、
LLMAttemptResult 统一记录。不 fork Atheris、不碰 State。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from stateforge.core.ir import ProgramIR
from stateforge.dynamic.mapper import map_trace
from stateforge.dynamic.tracer import TraceSession
from stateforge.feasibility.branch_observer import (Observer,
                                                    load_observed_target)
from stateforge.feedback.frontier_tracker import FrontierTracker
from stateforge.feasibility.guard_recovery import (_TestIndex,
                                                    recover_guard)
from stateforge.feasibility.input_provenance import (
    apply_transforms, resolve_operand_source)
from stateforge.feasibility.llm_solver import LLMSemanticSolver
from stateforge.feasibility.mutation_hint import MutationHint
from stateforge.feasibility.proposal import MutationProposal, ProposalStatus
from stateforge.feasibility.proposal_validator import validate
from stateforge.fuzz.constraint_mutator import (MutationError, apply_hint)
from stateforge.fuzz.corpus_manager import CorpusManager


@dataclass
class LLMAttemptResult:
    frontier_id: str
    invoked: bool = False
    proposal_valid: bool = False
    candidate_executed: bool = False
    coverage_before: int = 0
    coverage_after: int = 0
    new_blocks: int = 0
    new_edges: int = 0
    distance_before: float | None = None
    distance_after: float | None = None
    frontier_crossed: bool = False
    candidate_kept: bool = False
    failure_reason: str | None = None
    proposed_value: object = None


@dataclass
class CoverageLoopResult:
    target_reached: bool = False
    corpus_admitted: bool = False
    attempts: list[LLMAttemptResult] = field(default_factory=list)
    attempt_records: list[dict] = field(default_factory=list)
    final_seed: dict | None = None


class _Request:
    def __init__(self, seed: dict):
        self.args = dict(seed.get("query", {}))
        self.form = dict(seed.get("form", {}))
        self.json = dict(seed.get("json", {}))
        self.headers = dict(seed.get("header", {}))
        self.cookies = dict(seed.get("cookie", {}))
        self.values = {**self.args, **self.form}
        self.session = {"authenticated": False}


class LLMCoverageLoop:
    """极简 orchestrator:跑通顾问定义的完整闭环。"""

    def __init__(self, app_path: str | Path, seed: dict, provider,
                 workdir: str | Path,
                 target_function: str = "hidden",
                 max_attempts_per_frontier: int = 3,
                 max_total_calls: int = 50,
                 max_rounds: int = 10):
        from stateforge.static.icfg import build_icfg
        self.app_path = Path(app_path)
        self.seed = seed
        self.provider = provider
        self.solver = LLMSemanticSolver(provider)
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.ir = build_icfg(self.app_path.parent)
        self.tracker = FrontierTracker(self.ir)
        self.test_index = _TestIndex(self.ir)
        self.corpus = CorpusManager(self.workdir / "spool")
        self.target_function = target_function
        self.max_per_frontier = max_attempts_per_frontier
        self.max_total = max_total_calls
        self.max_rounds = max_rounds
        self.total_llm_calls = 0
        self.frontier_attempts: dict[str, int] = {}      # frontier_id → count
        self.frontier_failures: dict[str, list[str]] = {} # 失败值历史

        self._mod, self._observer = load_observed_target(self.app_path)
        self._session = TraceSession(root=str(self.app_path.parent))
        self._session.__enter__()
        self._target_blocks = {b.id for b in self.ir.blocks.values()
                               if b.function == target_function}

    def _execute(self, seed: dict):
        start_ev = len(self._session.events)
        start_obs = len(self._observer.records)
        with self._session.request():
            try:
                self._mod.run(_Request(seed))
            except Exception:                    # noqa: BLE001
                pass
        events = self._session.events[start_ev:]
        obs = self._observer.records[start_obs:]
        mapped = map_trace(events, self.ir)
        self.tracker.update(set(mapped.block_hits), set(mapped.edge_kinds))
        return obs, mapped

    def run(self) -> CoverageLoopResult:
        result = CoverageLoopResult()
        current = self.seed
        try:
            for _round in range(self.max_rounds):
                obs, mapped = self._execute(current)
                reached = bool(self._target_blocks & set(mapped.block_hits))
                if reached:
                    result.target_reached = True
                    result.final_seed = current
                    break

                # 找一条 stalled guarded frontier
                stalled = self._find_frontier()
                if stalled is None:
                    result.final_seed = current
                    break

                (u, v), guard = stalled
                fid = f"{u}->{v}"
                rec = self._try_llm(fid, u, v, guard, current, obs)
                result.attempts.append(rec)
                result.attempt_records.append({
                    "frontier_id": fid, "invoked": rec.invoked,
                    "valid": rec.proposal_valid,
                    "executed": rec.candidate_executed,
                    "crossed": rec.frontier_crossed,
                    "kept": rec.candidate_kept,
                    "value": str(rec.proposed_value)[:50],
                    "reason": rec.failure_reason})
                if rec.candidate_kept and rec.frontier_crossed:
                    current = self._last_candidate
                # 未跨越也继续下一轮(可能找别的 frontier 或重试)
            result.final_seed = result.final_seed or current
            result.corpus_admitted = len(self.corpus.entries()) > 0
        finally:
            self._session.__exit__(None, None, None)
        return result

    def _find_frontier(self):
        for (u, v) in self.tracker.edge_frontiers:
            edge_kind = next((e.kind for e in self.ir.edges
                              if e.src == u and e.dst == v), "true")
            guard = recover_guard(self.ir, u, v, edge_kind, self.test_index)
            if guard.status != "ok":
                continue
            fid = f"{u}->{v}"
            if self.frontier_attempts.get(fid, 0) >= self.max_per_frontier:
                continue
            return (u, v), guard
        return None

    def _try_llm(self, fid, u, v, guard, seed, obs) -> LLMAttemptResult:
        rec = LLMAttemptResult(frontier_id=fid)
        rec.coverage_before = len(self.tracker.covered_blocks)
        if self.total_llm_calls >= self.max_total:
            rec.failure_reason = "GLOBAL_BUDGET_EXHAUSTED"
            return rec

        # 构造 slice
        prov = resolve_operand_source(self.app_path,
                                      self.ir.blocks[u].function,
                                      guard.lineno)
        from stateforge.feasibility.semantic_slice import build_slice
        from stateforge.feasibility.stalled import StalledFrontier
        sf = StalledFrontier((u, v), 200, guard)  # 模拟足够命中(stall)
        slice_ = build_slice(self.ir, sf, obs, prov, self.app_path,
                             self.corpus_meta())
        # 喂失败历史(顾问 §十四:前次失败告诉 LLM)
        failures = self.frontier_failures.get(fid, [])
        if failures:
            slice_["previous_failed_values"] = failures

        self.total_llm_calls += 1
        self.frontier_attempts[fid] = self.frontier_attempts.get(fid, 0) + 1
        rec.invoked = True

        proposal, latency, tokens = self.solver.solve(slice_)
        if proposal.status != ProposalStatus.PROPOSED:
            rec.failure_reason = f"LLM_{proposal.status.value}"
            self.frontier_failures.setdefault(fid, []).append(
                str(proposal.proposed_value))
            return rec

        vres = validate(proposal, slice_)
        if not vres.valid:
            rec.failure_reason = f"VALIDATION_{vres.rejection_reason}"
            return rec
        rec.proposal_valid = True

        hint = MutationHint(frontier_id=fid, guard_id=guard.guard_id,
                            source_kind=proposal.source_kind,
                            source_key=proposal.source_key,
                            current_value=proposal.proposed_value,
                            target_value=proposal.proposed_value,
                            operator=guard.op,
                            desired_polarity=guard.polarity,
                            reason="solved",
                            confidence=proposal.confidence)
        try:
            candidate = apply_hint(seed, hint)
        except MutationError as exc:
            rec.failure_reason = f"MUTATION_{exc}"
            return rec

        # 真实执行
        self._last_candidate = candidate
        obs2, mapped2 = self._execute(candidate)
        rec.candidate_executed = True
        rec.coverage_after = len(self.tracker.covered_blocks)
        rec.new_blocks = rec.coverage_after - rec.coverage_before
        rec.new_edges = len(self.tracker.covered_edges) - len(
            {e for e in self.tracker.covered_edges})  # simplified
        rec.frontier_crossed = (u, v) in set(mapped2.edge_kinds)

        if rec.new_blocks > 0 or rec.frontier_crossed:
            cid = self.corpus.save(
                json.dumps(candidate, sort_keys=True).encode(),
                epoch=0, new_blocks=rec.new_blocks, new_edges=0,
                discovered_blocks=rec.coverage_after,
                discovered_edges=0,
                distance=rec.distance_after,
                nearest_frontier=fid,
                interesting_reason="llm_proposal")
            rec.candidate_kept = cid is not None
        else:
            self.frontier_failures.setdefault(fid, []).append(
                str(proposal.proposed_value))
            rec.failure_reason = "NO_COVERAGE_GAIN"
        return rec

    def corpus_meta(self):
        return [{"input_id": e.input_id, "distance": e.distance,
                 "interesting_reason": e.interesting_reason,
                 "nearest_frontier": e.nearest_frontier}
                for e in self.corpus.entries()]
