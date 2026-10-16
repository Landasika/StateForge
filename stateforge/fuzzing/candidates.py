"""Phase-2 模块 3:候选采集器——TestOneInput 入口捕获字节并评结构新颖度。

在 handler 内,逐输入:执行(TraceSession.request 隔离)→ map_trace →
observe(对执行前全局快照)→ interesting 则 CorpusManager.save 原始字节 →
update_global_coverage → Recorder.tick(含执行/反馈分项计时)。

阶段 1(P0-A/P0-B)接线:
- 可选 ``execution``(ExecutionContext):输入级状态隔离生命周期
  (默认 None → Noop,行为与旧版一致——输入间状态延续是钉扎语义);
- 可选 ``state_ledger``(StateEvidenceLedger):记录运行时 verified 状态
  证据(与覆盖反馈并记,互不替代);
- 生命周期失败(LifecycleError):审计落 lifecycle_failures.jsonl,
  **该输入不计执行、不计覆盖、不入语料**(fail-closed,不静默继续);
  观测/还原在 trace 窗口外执行(观测器帧不得进覆盖计数)。
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
from stateforge.fuzzing.feedback.structural import StructuralFeedback
from stateforge.fuzzing.corpus_manager import CorpusManager


class CandidateCollector:
    def __init__(self, ir: ProgramIR, session: TraceSession,
                 feedback: StructuralFeedback,
                 corpus: CorpusManager | None,       # None = measure 模式
                 recorder: ExperimentRecorder, epoch: int,
                 on_save=None,                      # P0-7:保存时持久化全局覆盖
                 execution: ExecutionContext | None = None,   # P0-A 生命周期
                 state_ledger=None):                # P0-B 证据台账
        self.ir = ir
        self.session = session
        self.feedback = feedback
        self.corpus = corpus
        self.recorder = recorder
        self.epoch = epoch
        self.on_save = on_save
        self.execution = execution if execution is not None else ExecutionContext()
        self.state_ledger = state_ledger
        self.lifecycle_failures = 0         # 隔离失败次数(审计之外的记忆)

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
            except Exception:                      # noqa: BLE001  fuzz 输入不得中断引擎
                pass
            exec_s = time.monotonic() - t0
        # P0-A/P0-B:观测(还原前)+ 基线还原。TraceSession.request() 只切
        # 换事件归属、不停止收集(tracer.untraced docstring),故此处必须
        # 显式挂起追踪——观测器/还原帧是反馈机制自身,不得混进覆盖计数;
        # 还原失败同样 fail-closed。
        try:
            with self.session.untraced():
                result = self.execution.finish_run(data)
        except LifecycleError as exc:
            self._lifecycle_failed(exc)
            return None
        if self.state_ledger is not None:
            self.state_ledger.record(result.observations)
        t1 = time.monotonic()
        mapped = map_trace(self.session.events[start:], self.ir)
        obs = self.feedback.observe(mapped)         # 对执行前快照评分
        cid = None
        if self.corpus is not None and obs.interesting:
            cid = self.corpus.save(data, self.epoch, len(obs.new_blocks),
                                   len(obs.new_edges), len(obs.blocks),
                                   len(obs.edges))
            if cid and self.on_save:
                self.on_save()                       # 立刻落盘,抗 _Exit 尾丢
        self.feedback.update_global_coverage(obs.blocks, obs.edges)
        feedback_s = time.monotonic() - t1
        self.recorder.tick(exec_s, feedback_s,
                           len(self.feedback.global_blocks),
                           len(self.feedback.global_edges),
                           cid is not None, cid, obs.score)
        return obs
