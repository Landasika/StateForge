"""Directed greybox campaign: schedule → mutate → execute → score → retain.

一次 campaign = 围绕一个静态目标块的固定预算搜索。每次执行都走:
adapter 执行(经 ``trace_execution`` + 独立 ``request`` 窗口)→ 轨迹按请求
排空 → ``map_trace`` 映射到 ICFG → 算到目标块的静态距离 → 语料库决定是否
保留 → 记录一行 metrics。

三条口径在这一层被严格分开,不可互换:

* ``distance == 0`` / ``target_reached`` —— 真的命中目标块;
* ``nearest_block`` —— 只说明"本次最近走到哪",**不是**命中;
* ``observed_valid_edges`` vs ``unmatched_transitions`` —— 前者是 ICFG 上
  真实存在的边,后者只是"两块先后执行",不能计入结构覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import random
import time
from typing import Any, Iterable

from stateforge.core.ir import ProgramIR
from stateforge.fuzzing.feedback.corpus import (
    GuidedCorpus,
    GuidedSeed,
    seed_id_for,
)
from stateforge.fuzzing.guidance.mutator import BasicMutator
from stateforge.fuzzing.guidance.results import (
    environment_info,
    CampaignReporter,
    CampaignResult,
)
from stateforge.fuzzing.guidance.scheduler import (
    EmptyCorpusError,
    GuidedScheduler,
    UniformScheduler,
)
from stateforge.fuzzing.guidance.target import (
    BasicBlockTarget,
    TargetManager,
)
from stateforge.runtime.mapper import map_trace
from stateforge.runtime.execution import execute_traced
from stateforge.runtime.execution_context import ExecutionContext
from stateforge.runtime.tracer import TraceSession


@dataclass
class ExecutionResult:
    input_id: str
    parent_id: str | None
    covered_blocks: frozenset[str]
    covered_edges: frozenset[tuple[str, str]]
    distance: int | None
    target_reached: bool
    execution_time_ms: float
    error: str | None
    trace_events: int = 0
    unresolved_events: int = 0
    nearest_block: str | None = None
    valid_edge_count: int = 0
    unmatched_edge_count: int = 0


@dataclass
class CampaignConfig:
    max_executions: int = 10000
    max_seconds: float | None = None
    max_input_size: int = 4096
    max_corpus_size: int = 1000
    epsilon: float = 0.20
    random_seed: int = 0
    stop_on_target: bool = True
    fault_policy: str = "continue"
    allow_stale_ir: bool = False
    output_dir: str | Path | None = None
    min_energy: int = 1
    max_energy: int = 32
    # ``scheduler_name`` 是"选种+能量"的合并开关(旧口径);显式给了
    # selection/energy_policy 时以后者为准,两者的组合在 __post_init__ 校验。
    scheduler_name: str | None = None
    selection_policy: str | None = None
    energy_policy: str | None = None
    fixed_energy: int = 1
    progress_every: int = 100

    def __post_init__(self) -> None:
        defaults = {None: ("distance", "distance"),
                    "guided": ("distance", "distance"),
                    "uniform": ("uniform", "fixed")}
        if self.scheduler_name not in defaults:
            raise ValueError("scheduler_name must be 'guided' or 'uniform'")
        selection, energy = defaults[self.scheduler_name]
        if self.selection_policy is None:
            self.selection_policy = selection
        if self.energy_policy is None:
            self.energy_policy = energy
        if self.selection_policy not in ("uniform", "distance"):
            raise ValueError("selection_policy must be 'uniform' or 'distance'")
        if self.energy_policy not in ("fixed", "distance"):
            raise ValueError("energy_policy must be 'fixed' or 'distance'")
        if self.fixed_energy < 1:
            raise ValueError("fixed_energy must be positive")
        if self.max_executions < 0:
            raise ValueError("max_executions must be non-negative")
        if self.max_seconds is not None and self.max_seconds < 0:
            raise ValueError("max_seconds must be non-negative")
        if self.max_input_size < 0:
            raise ValueError("max_input_size must be non-negative")
        if self.max_corpus_size < 1:
            raise ValueError("max_corpus_size must be positive")
        if not 0.0 <= self.epsilon <= 1.0:
            raise ValueError("epsilon must be in [0, 1]")
        if self.fault_policy not in ("continue", "stop"):
            raise ValueError("fault_policy must be 'continue' or 'stop'")
        if self.min_energy < 1 or self.max_energy < self.min_energy:
            raise ValueError("invalid energy bounds")
        if self.progress_every < 1:
            raise ValueError("progress_every must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_executions": self.max_executions,
            "max_seconds": self.max_seconds,
            "max_input_size": self.max_input_size,
            "max_corpus_size": self.max_corpus_size,
            "epsilon": self.epsilon,
            "random_seed": self.random_seed,
            "stop_on_target": self.stop_on_target,
            "fault_policy": self.fault_policy,
            "allow_stale_ir": self.allow_stale_ir,
            "output_dir": str(self.output_dir) if self.output_dir else None,
            "min_energy": self.min_energy,
            "max_energy": self.max_energy,
            "scheduler": self.selection_policy,
            "selection_policy": self.selection_policy,
            "energy_policy": self.energy_policy,
            "fixed_energy": self.fixed_energy,
            "legacy_scheduler_name": self.scheduler_name,
            "progress_every": self.progress_every,
        }


class GuidedCampaign:
    def __init__(self, ir: ProgramIR, target: BasicBlockTarget, adapter: Any,
                 corpus: GuidedCorpus | None = None,
                 scheduler: GuidedScheduler | UniformScheduler | None = None,
                 mutator: BasicMutator | None = None,
                 config: CampaignConfig | None = None,
                 session: TraceSession | None = None):
        self.config = config if config is not None else CampaignConfig()
        self.ir = ir
        self.target_manager = TargetManager(
            ir, target, allow_stale=self.config.allow_stale_ir)
        self.target = target
        self.adapter = adapter
        if not callable(getattr(adapter, "execute", None)):
            raise ValueError("adapter must provide execute(data)")
        self.corpus = corpus if corpus is not None else GuidedCorpus(
            self.config.max_corpus_size)
        if scheduler is None:
            scheduler = GuidedScheduler(
                epsilon=self.config.epsilon,
                random_seed=self.config.random_seed,
                min_energy=self.config.min_energy,
                max_energy=self.config.max_energy,
                selection_policy=self.config.selection_policy,
                energy_policy=self.config.energy_policy,
                fixed_energy=self.config.fixed_energy)
        self.scheduler = scheduler
        # Structured mutation is opt-in: pass a codec (or a pre-built mutator)
        # explicitly to enable it.
        #
        # It is not the default because the trade-off is unresolved. Mutating a
        # parsed request keeps 92% of outputs parseable against 26% for byte
        # mode, but on microblog the same 300-execution budget covered 115
        # blocks instead of 255. The cause is that an unparseable input falls
        # back to the adapter's default request, which repeatedly renders the
        # heaviest page: byte mode is effectively a homepage hammer, and block
        # count rewards that. Whether structured mode is better depends on
        # measuring per-route depth rather than raw block totals, so switching
        # the default before that measurement would trade one unvalidated
        # behaviour for another.
        self.mutator = (mutator if mutator is not None
                        else BasicMutator(self.config.random_seed))
        root = ir.metadata.get("root")
        self.session = (session if session is not None
                        else TraceSession(root if root else None))
        self.execution = ExecutionContext()
        self._valid_edges = {(edge.src, edge.dst) for edge in ir.edges}
        self._rng = random.Random(self.config.random_seed ^ 0x5F3759DF)
        self.initial_seeds: list[bytes] = []
        self.executions = 0
        self.total_trace_events = 0
        self.mapping_failures = 0
        self.unexpected_exceptions = 0
        self.best_distance: int | None = None
        self.executions_to_target: int | None = None
        self.wall_clock_to_target: float | None = None
        self.target_input_id: str | None = None
        self.termination_reason: str | None = None
        self._stopped = False
        self._start_time = time.monotonic()
        self._reporter = (CampaignReporter(
            self.config.output_dir, self.config, target.block_id,
            ir.metadata.get("source_hash"))
            if self.config.output_dir is not None else None)

    def add_seed(self, data: bytes) -> None:
        self.initial_seeds.append(bytes(data))

    def execute_input(self, data: bytes, parent_id: str | None = None,
                      input_id: str | None = None) -> ExecutionResult:
        self.executions += 1
        if input_id is None:
            input_id = f"candidate_{self.executions}"
        started = time.perf_counter()
        events = []
        error: str | None = None
        try:
            # 适配器自身的重置/观测属于反馈机制,不是被测执行——挂起采集,
            # 否则会把"反馈自己跑过的代码"算成候选输入的覆盖。
            reset = getattr(self.adapter, "reset", None)
            if callable(reset):
                with self.session.untraced():
                    reset()
            run = execute_traced(self.adapter.execute, data, self.session, self.execution)
            events = run.events
            if run.error is not None:
                error = f"{type(run.error).__name__}: {run.error}"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        execution_time_ms = (time.perf_counter() - started) * 1000.0
        mapped = map_trace(events, self.ir)
        reached = self.target_manager.reached(mapped)
        distance = (0 if reached
                    else self.target_manager.distance_for_trace(mapped))
        covered_blocks = frozenset(mapped.blocks)
        # 只把静态图上真实存在的边算作覆盖;相邻但无静态边的是
        # unmatched,单独计数,不能进 covered_edges。
        covered_edges = frozenset(pair for pair in mapped.edges
                                  if pair in self._valid_edges)
        self.total_trace_events += len(events)
        self.mapping_failures += len(mapped.unresolved)
        if error is not None:
            self.unexpected_exceptions += 1
        return ExecutionResult(
            input_id=input_id,
            parent_id=parent_id,
            covered_blocks=covered_blocks,
            covered_edges=covered_edges,
            distance=distance,
            target_reached=reached,
            execution_time_ms=execution_time_ms,
            error=error,
            trace_events=len(events),
            unresolved_events=len(mapped.unresolved),
            nearest_block=self.target_manager.nearest_block(mapped),
            valid_edge_count=len(covered_edges),
            unmatched_edge_count=len(mapped.unmatched_transitions),
        )

    def run(self, initial_seeds: Iterable[bytes] | None = None) -> CampaignResult:
        seeds = ([bytes(seed) for seed in initial_seeds]
                 if initial_seeds is not None else list(self.initial_seeds))
        if not seeds and len(self.corpus) == 0:
            if self._reporter is not None:
                self._reporter.close()
            raise EmptyCorpusError("campaign requires at least one initial seed")
        self._start_time = time.monotonic()
        if self._reporter is not None:
            identities = [seed_id_for(seed) for seed in seeds]
            identities.extend(seed.seed_id for seed in self.corpus.seeds)
            self._reporter.start(identities, self._scheduler_info(),
                                 self._mutation_info())
        try:
            for data in seeds:
                reason = self._budget_reason()
                if reason is not None:
                    self._reason(reason)
                    break
                self._evaluate(data, None, seed_id_for(data))
                if self._stopped:
                    break
            if not self._stopped:
                self._fuzz_loop()
            return self._result(self.termination_reason or "completed")
        finally:
            if self._reporter is not None:
                try:
                    self._save_target_archive()
                    self._reporter.write_summary(
                        self._summary(self.termination_reason or "error"))
                finally:
                    self._reporter.close()

    def _fuzz_loop(self) -> None:
        while True:
            reason = self._budget_reason()
            if reason is not None:
                self._reason(reason)
                return
            parent = self.scheduler.select(self.corpus)
            energy = self.scheduler.allocate_energy(parent)
            for _ in range(energy):
                reason = self._budget_reason()
                if reason is not None:
                    self._reason(reason)
                    return
                other = None
                if len(self.corpus) > 1:
                    donors = [seed for seed in self.corpus.seeds
                              if seed.seed_id != parent.seed_id]
                    if donors:
                        other = self._rng.choice(donors).data
                candidate = self.mutator.mutate(
                    parent.data,
                    max_size=self.config.max_input_size,
                    other=other,
                )
                input_id = f"candidate_{self.executions + 1}"
                self._evaluate(candidate, parent, input_id)
                if self._stopped:
                    return

    def _evaluate(self, data: bytes, parent: GuidedSeed | None,
                  input_id: str) -> ExecutionResult:
        parent_id = parent.seed_id if parent is not None else None
        result = self.execute_input(data, parent_id=parent_id, input_id=input_id)
        new_blocks, new_edges = self.corpus.novelty(result)
        retained = self.corpus.consider(
            data, result, parent_id=parent_id)
        seed = self.corpus.get(seed_id_for(data))
        if retained and seed is not None and self._reporter is not None:
            self._reporter.save_seed(seed)
        if result.error is not None and self._reporter is not None:
            self._reporter.save_crash(result.input_id, data, result.error)
        if result.target_reached:
            if self.executions_to_target is None:
                self.executions_to_target = self.executions
                self.wall_clock_to_target = (
                    time.monotonic() - self._start_time)
                self.target_input_id = result.input_id
                if self._reporter is not None:
                    self._reporter.save_target_input(result.input_id, data)
            if self.config.stop_on_target:
                self._reason("target_reached")
        if parent is not None:
            self.scheduler.update(parent, result)
        self.best_distance = self.corpus.best_distance
        if result.error is not None and self.config.fault_policy == "stop":
            self._reason("fault")
        record = {
            "execution": self.executions,
            "selected_seed_id": parent_id,
            "candidate_id": result.input_id,
            "parent_distance": parent.distance if parent is not None else None,
            "candidate_distance": result.distance,
            "best_distance": self.corpus.best_distance,
            "new_blocks": len(new_blocks),
            "new_edges": len(new_edges),
            "retained": retained,
            "target_reached": result.target_reached,
            # 只有"真的比父种更近"才算改善;命中目标恒为改善。
            "distance_improved": (
                bool(result.target_reached) or (
                    parent is not None
                    and parent.distance is not None
                    and result.distance is not None
                    and result.distance < parent.distance)),
            "nearest_block": result.nearest_block,
            "valid_edge_count": result.valid_edge_count,
            "unmatched_edge_count": result.unmatched_edge_count,
            "error": result.error,
            "execution_time_ms": result.execution_time_ms,
            "trace_events": result.trace_events,
            "mapping_failures": result.unresolved_events,
        }
        if self._reporter is not None:
            self._reporter.record(record)
            if self.executions % self.config.progress_every == 0:
                self._reporter.write_summary(
                    self._summary(self.termination_reason or "running"))
        return result

    def _save_target_archive(self) -> None:
        if self._reporter is None:
            return
        for seed in self.corpus.archived_targets:
            self._reporter.save_archived_target(seed)

    def _budget_reason(self) -> str | None:
        if self.executions >= self.config.max_executions:
            return "max_executions"
        if (self.config.max_seconds is not None
                and time.monotonic() - self._start_time
                >= self.config.max_seconds):
            return "max_seconds"
        return None

    def _reason(self, reason: str) -> None:
        if self.termination_reason is None:
            self.termination_reason = reason
        self._stopped = True

    def _result(self, reason: str) -> CampaignResult:
        elapsed = time.monotonic() - self._start_time
        valid_edges = {edge for edge in self.corpus.covered_edges
                       if edge in self._valid_edges}
        return CampaignResult(
            target_block_id=self.target.block_id,
            target_reached=self.corpus.target_reached,
            executions=self.executions,
            executions_to_target=self.executions_to_target,
            wall_clock_to_target=self.wall_clock_to_target,
            best_distance=self.corpus.best_distance,
            unique_covered_blocks=len(self.corpus.covered_blocks),
            unique_valid_icfg_edges=len(valid_edges),
            retained_seeds=len(self.corpus),
            unexpected_exceptions=self.unexpected_exceptions,
            mapping_failures=self.mapping_failures,
            mapping_failure_rate=(self.mapping_failures
                                  / max(self.total_trace_events, 1)),
            termination_reason=reason,
            elapsed_seconds=elapsed,
            target_input_id=self.target_input_id,
            output_dir=(str(self.config.output_dir)
                        if self.config.output_dir is not None else None),
            archived_targets=len(self.corpus.target_archive),
        )

    def _summary(self, reason: str) -> dict[str, Any]:
        result = self._result(reason)
        payload = result.to_dict()
        payload["scheduler"] = self._scheduler_info()
        payload["mutation"] = self._mutation_info()
        payload["corpus_size"] = len(self.corpus)
        payload["total_trace_events"] = self.total_trace_events
        payload["source_hash"] = self.ir.metadata.get("source_hash")
        payload["target_input_path"] = (
            str(self._reporter.target_input_path)
            if self.corpus.target_reached and self._reporter is not None
            else None)
        payload["execution_throughput_per_second"] = (
            self.executions / result.elapsed_seconds
            if result.elapsed_seconds > 0 else 0.0)
        payload["config"] = self.config.to_dict()
        # summary 必须能独立佐证结果:换机器/换依赖后数字不可比,
        # 故环境指纹与初始语料一并带上,不要求读者再去翻 config.json。
        payload["environment"] = environment_info()
        payload["initial_corpus"] = [seed_id_for(seed)
                                     for seed in self.initial_seeds]
        return payload

    def _scheduler_info(self) -> dict[str, Any]:
        selection_policy = getattr(self.scheduler, "selection_policy",
                                   self.config.selection_policy)
        energy_policy = getattr(self.scheduler, "energy_policy",
                                self.config.energy_policy)
        fixed_energy = getattr(self.scheduler, "fixed_energy",
                               self.config.fixed_energy)
        return {
            "name": selection_policy,
            "selection_policy": selection_policy,
            "energy_policy": energy_policy,
            "fixed_energy": fixed_energy,
            "epsilon": self.config.epsilon,
            "random_seed": self.config.random_seed,
            "min_energy": self.config.min_energy,
            "max_energy": self.config.max_energy,
        }

    def _mutation_info(self) -> dict[str, Any]:
        return {
            "operators": list(self.mutator.operators),
            "random_seed": self.mutator.random_seed,
            "max_input_size": self.config.max_input_size,
        }
