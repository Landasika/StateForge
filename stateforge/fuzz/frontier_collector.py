"""Phase-3.1 采集器:frontier 模式 handler(NB+NE+1/(1+D),全局冠军保留)。"""

from __future__ import annotations

import time

from stateforge.core.ir import ProgramIR
from stateforge.dynamic.mapper import map_trace
from stateforge.dynamic.tracer import TraceSession
from stateforge.experiment.recorder import ExperimentRecorder
from stateforge.feedback.frontier_feedback import FrontierDirectedFeedback
from stateforge.fuzz.corpus_manager import CorpusManager


class FrontierCollector:
    def __init__(self, ir: ProgramIR, session: TraceSession,
                 feedback: FrontierDirectedFeedback, corpus: CorpusManager,
                 recorder: ExperimentRecorder, epoch: int, on_save=None):
        self.ir, self.session, self.feedback = ir, session, feedback
        self.corpus, self.recorder, self.epoch = corpus, recorder, epoch
        self.on_save = on_save
        self.frontier_saves = 0          # reason=frontier_proximity 的保留数
        self._events_path = corpus.spool / "frontier_events.jsonl"

    def handle(self, target, data: bytes):
        start = len(self.session.events)
        with self.session.request():
            t0 = time.monotonic()
            try:
                target(data)
            except Exception:                      # noqa: BLE001
                pass
            exec_s = time.monotonic() - t0
        t1 = time.monotonic()
        mapped = map_trace(self.session.events[start:], self.ir)
        obs = self.feedback.observe(mapped)         # 旧前沿上评分
        cid = None
        if obs.interesting:
            cid = self.corpus.save(data, self.epoch, obs.new_blocks,
                                   obs.new_edges, len(mapped.block_hits),
                                   len(mapped.edge_kinds),
                                   distance=obs.distance,
                                   nearest_frontier=obs.nearest_frontier,
                                   interesting_reason=obs.reason)
            if cid and obs.reason == "frontier_proximity":
                self.frontier_saves += 1
            if cid and self.on_save:
                self.on_save()
        self.feedback.commit(mapped, cid is not None)
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
