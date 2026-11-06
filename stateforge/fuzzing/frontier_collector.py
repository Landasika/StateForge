"""Phase-3.1 采集器:frontier 模式 handler(NB+NE+1/(1+D),全局冠军保留)。

阶段 1(P0-A/P0-B)接线(与 CandidateCollector 同形):
- 可选 ``execution``(ExecutionContext)/``state_ledger``;旧覆盖前沿反馈
  **保留原样**,状态证据与之并记(不是替代);
- 生命周期失败(LifecycleError):审计落 lifecycle_failures.jsonl,该输入
  不计执行/覆盖/语料/前沿(fail-closed);观测与还原在 trace 窗口外。
"""

from __future__ import annotations

import time

from stateforge.fuzzing.collector import Collector, CollectedExecution
from stateforge.fuzzing.corpus_manager import CorpusManager


class FrontierCollector(Collector):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.frontier_saves = 0
        self._events_path = self.corpus.spool / "frontier_events.jsonl"

    def _process(self, data: bytes, run: CollectedExecution):
        mapped = run.mapped
        state_novel = run.state_novel
        exec_s, t1 = run.execution_seconds, run.feedback_started
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
