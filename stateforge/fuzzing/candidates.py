"""Phase-2 模块 3:候选采集器——TestOneInput 入口捕获字节并评结构新颖度。

在 handler 内,逐输入:执行(TraceSession.request 隔离)→ map_trace →
observe(对执行前全局快照)→ interesting 则 CorpusManager.save 原始字节 →
update_global_coverage → Recorder.tick(含执行/反馈分项计时)。
"""

from __future__ import annotations

import time

from stateforge.core.ir import ProgramIR
from stateforge.runtime.mapper import map_trace
from stateforge.runtime.tracer import TraceSession
from stateforge.evaluation.experiment.recorder import ExperimentRecorder
from stateforge.fuzzing.feedback.structural import StructuralFeedback
from stateforge.fuzzing.corpus_manager import CorpusManager


class CandidateCollector:
    def __init__(self, ir: ProgramIR, session: TraceSession,
                 feedback: StructuralFeedback,
                 corpus: CorpusManager | None,       # None = measure 模式
                 recorder: ExperimentRecorder, epoch: int,
                 on_save=None):                      # P0-7:保存时持久化全局覆盖
        self.ir = ir
        self.session = session
        self.feedback = feedback
        self.corpus = corpus
        self.recorder = recorder
        self.epoch = epoch
        self.on_save = on_save

    def handle(self, target, data: bytes):
        start = len(self.session.events)
        with self.session.request():
            t0 = time.monotonic()
            try:
                target(data)
            except Exception:                      # noqa: BLE001  fuzz 输入不得中断引擎
                pass
            exec_s = time.monotonic() - t0
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
