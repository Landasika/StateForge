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

from stateforge.fuzzing.collector import Collector, CollectedExecution
from stateforge.fuzzing.corpus_manager import CorpusManager


class CandidateCollector(Collector):
    def _process(self, data: bytes, run: CollectedExecution):
        mapped = run.mapped
        state_novel = run.state_novel
        exec_s, t1 = run.execution_seconds, run.feedback_started
        obs = self.feedback.observe(mapped)         # 对执行前快照评分
        cid = None
        keep_for_state = self.state_novelty and bool(state_novel)
        if self.corpus is not None and (obs.interesting or keep_for_state):
            cid = self.corpus.save(
                data, self.epoch, len(obs.new_blocks),
                len(obs.new_edges), len(obs.blocks), len(obs.edges),
                interesting_reason=("state_novel" if keep_for_state
                                    and not obs.interesting
                                    else "new_structure"))
            if cid and self.on_save:
                self.on_save()                       # 立刻落盘,抗 _Exit 尾丢
        self.feedback.update_global_coverage(obs.blocks, obs.edges)
        feedback_s = time.monotonic() - t1
        self.recorder.tick(exec_s, feedback_s,
                           len(self.feedback.global_blocks),
                           len(self.feedback.global_edges),
                           cid is not None, cid, obs.score)
        return obs
