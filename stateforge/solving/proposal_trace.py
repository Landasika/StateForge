"""Phase-4.2-D:Proposal Trace——逐次 LLM 调用的完整审计记录。"""

from __future__ import annotations

import json
import time
from pathlib import Path


class ProposalTraceRecorder:
    """JSONL 落盘,逐条 flush(抗 _Exit);离线可聚合指标。"""

    FIELDS = ("trigger_reason", "frontier_id", "slice_hash",
              "deterministic_failure_reason", "llm_status", "proposal",
              "validation_status", "candidate_generated", "candidate_executed",
              "distance_before", "distance_after", "branch_satisfied",
              "frontier_crossed", "latency_seconds", "token_usage")

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")

    def record(self, **kw) -> None:
        row = {k: kw.get(k) for k in self.FIELDS}
        row["ts"] = time.time()
        self._fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass
