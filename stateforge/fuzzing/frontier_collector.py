"""Phase-3.1 采集器:frontier 模式 handler(NB+NE+1/(1+D),全局冠军保留)。

阶段 1(P0-A/P0-B)接线(与 CandidateCollector 同形):
- 可选 ``execution``(ExecutionContext)/``state_ledger``;旧覆盖前沿反馈
  **保留原样**,状态证据与之并记(不是替代);
- 生命周期失败(LifecycleError):审计落 lifecycle_failures.jsonl,该输入
  不计执行/覆盖/语料/前沿(fail-closed);观测与还原在 trace 窗口外。
"""

from __future__ import annotations

import time

from stateforge.core.ir import ProgramIR
from stateforge.runtime.execution_context import (
    ExecutionContext,
    LifecycleError,
    audit_lifecycle_failure,
)
from stateforge.runtime.mapper import map_trace
from stateforge.runtime.tracer import TraceSession
from stateforge.fuzzing.recorder import ExperimentRecorder
from stateforge.fuzzing.feedback.frontier_feedback import FrontierDirectedFeedback
from stateforge.fuzzing.corpus_manager import CorpusManager


class FrontierCollector:
    def __init__(self, ir: ProgramIR, session: TraceSession,
                 feedback: FrontierDirectedFeedback, corpus: CorpusManager,
                 recorder: ExperimentRecorder, epoch: int, on_save=None,
                 execution: ExecutionContext | None = None,   # P0-A 生命周期
                 state_ledger=None,                # P0-B 证据台账
                 state_novelty: bool = False):    # PR-07 状态新颖度保留(默认关)
        self.ir, self.session, self.feedback = ir, session, feedback
        self.corpus, self.recorder, self.epoch = corpus, recorder, epoch
        self.on_save = on_save
        self.execution = execution if execution is not None else ExecutionContext()
        self.state_ledger = state_ledger
        self.state_novelty = state_novelty          # 默认关:A–F 不变
        self.frontier_saves = 0          # reason=frontier_proximity 的保留数
        self.lifecycle_failures = 0      # 隔离失败次数(审计之外的记忆)
        self._events_path = corpus.spool / "frontier_events.jsonl"

    def _lifecycle_failed(self, exc: LifecycleError):
        """fail-closed 审计:写 jsonl + 计数;调用方跳过该输入。"""
        self.lifecycle_failures += 1
        audit_lifecycle_failure(self.recorder.dir, {
            "group": self.recorder.group, "epoch": self.epoch,
            "lifecycle": self.execution.lifecycle.name,
            "isolation": self.execution.lifecycle.isolation,
            "error": str(exc)})

    def handle(self, target, data: bytes):
        start = len(self.session.events)
        with self.session.request():
            t0 = time.monotonic()
            try:
                self.execution.begin_run()           # P0-A:失败 → 未执行
            except LifecycleError as exc:
                self._lifecycle_failed(exc)
                return None
            try:
                target(data)
            except Exception:                      # noqa: BLE001
                pass
            exec_s = time.monotonic() - t0
        # P0-A/P0-B:观测(还原前)+ 基线还原。挂起追踪(tracer.untraced
        # docstring:request() 只切换归属不停止收集),观测/还原帧不入覆盖。
        try:
            with self.session.untraced():
                result = self.execution.finish_run(data)
        except LifecycleError as exc:
            self._lifecycle_failed(exc)
            return None
        state_novel = ()
        if self.state_ledger is not None:
            state_novel = self.state_ledger.record(result.observations)
        t1 = time.monotonic()
        mapped = map_trace(self.session.events[start:], self.ir)
        cid_pre = CorpusManager.content_id(data)
        obs = self.feedback.observe(mapped, cid_pre)   # 旧前沿上评分+组合挑战
        cid = None
        keep_for_state = self.state_novelty and bool(state_novel)
        if obs.interesting or keep_for_state:
            cid = self.corpus.save(
                data, self.epoch, obs.new_blocks,
                obs.new_edges, len(mapped.block_hits),
                len(mapped.edge_kinds),
                distance=obs.distance,
                nearest_frontier=obs.nearest_frontier,
                interesting_reason=("state_novel" if keep_for_state
                                    and not obs.interesting
                                    else obs.reason))
            if cid and obs.reason in ("frontier_champion", "frontier_progress"):
                self.frontier_saves += 1
            if cid and self.on_save:
                self.on_save()
        self.feedback.commit(mapped, cid is not None)
        for ev in self.feedback.portfolio.drain_events():
            self._events_path.parent.mkdir(parents=True, exist_ok=True)
            with self._events_path.open("a", encoding="utf-8") as fh:
                fh.write(__import__("json").dumps(vars(ev) | {"edge": None},
                                                  ensure_ascii=False) + "\n")
        for ev in self.feedback.tracker.drain_lifecycle():
            self._events_path.parent.mkdir(parents=True, exist_ok=True)
            with self._events_path.open("a", encoding="utf-8") as fh:
                fh.write(__import__("json").dumps(ev, ensure_ascii=False) + "\n")
        feedback_s = time.monotonic() - t1
        self.recorder.tick(exec_s, feedback_s,
                           len(self.feedback.novelty.global_blocks),
                           len(self.feedback.novelty.global_edges),
                           cid is not None, cid, int(obs.score))
        return obs
