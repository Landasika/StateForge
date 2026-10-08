"""Phase-2 模块 5:实验记录器——覆盖-时间采样 + 反馈开销。

两类记录(均逐行 flush,抗 libFuzzer _Exit):
- snapshots.jsonl:按时间间隔的 (t, executions, |B|, |E|) 采样
- candidates.jsonl:候选保存事件(input_id/epoch/score)
epoch 汇总由 summarize() 在内存生成,由 EpochController 落盘。

P3-T4(演进级,C9):tick 增可选 state_atoms_reached/state_injections
——轮间接缝的状态记账(已达状态原子数 / 序列种子注入数),非逐执行量:
缺省 None 时键不出现(snapshots 8 键、summary 11 键不变);一经传入即
粘滞(后续采样与汇总持续携带),由调用方在状态边界更新。
"""

from __future__ import annotations

import json
import time
from pathlib import Path


class ExperimentRecorder:
    def __init__(self, out_dir: str | Path, group: str, epoch: int,
                 sample_interval: float = 2.0, on_sample=None):
        self.dir = Path(out_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.group = group
        self.epoch = epoch
        self._t0 = time.monotonic()
        self._last_sample = 0.0
        self.sample_interval = sample_interval
        self.on_sample = on_sample
        self.executions = 0
        self.exec_seconds = 0.0          # 目标执行累计(含 settrace)
        self.feedback_seconds = 0.0      # map+observe+save 累计(反馈开销)
        self.candidates_saved = 0
        self.state_atoms_reached: int | None = None   # 演进级:轮间状态记账
        self.state_injections: int | None = None
        self.blocks_now = 0
        self.edges_now = 0
        self._snap = (self.dir / f"{group}-e{epoch}-snapshots.jsonl").open(
            "w", encoding="utf-8")
        self._cand = (self.dir / f"{group}-e{epoch}-candidates.jsonl").open(
            "a", encoding="utf-8")

    def tick(self, exec_s: float, feedback_s: float, blocks: int, edges: int,
             saved: bool, cid: str | None, score: int,
             state_atoms_reached: int | None = None,
             state_injections: int | None = None) -> None:
        self.executions += 1
        self.exec_seconds += exec_s
        self.feedback_seconds += feedback_s
        self.blocks_now = blocks
        self.edges_now = edges
        # P3-T4 演进级:状态边界量,None=不涉及(键集不变);传入即粘滞
        if state_atoms_reached is not None:
            self.state_atoms_reached = state_atoms_reached
        if state_injections is not None:
            self.state_injections = state_injections
        if saved:
            self.candidates_saved += 1
            self._cand.write(json.dumps(
                {"input_id": cid, "epoch": self.epoch, "score": score},
                ensure_ascii=False) + "\n")
            self._cand.flush()
        now = time.monotonic() - self._t0
        if now - self._last_sample >= self.sample_interval:
            self._last_sample = now
            snap = {
                "group": self.group, "epoch": self.epoch, "t": round(now, 2),
                "executions": self.executions,
                "blocks": self.blocks_now, "edges": self.edges_now,
                "exec_seconds": round(self.exec_seconds, 3),
                "feedback_seconds": round(self.feedback_seconds, 3),
            }
            if self.state_atoms_reached is not None:
                snap["state_atoms_reached"] = self.state_atoms_reached
            if self.state_injections is not None:
                snap["state_injections"] = self.state_injections
            self._snap.write(json.dumps(snap, ensure_ascii=False) + "\n")
            self._snap.flush()
            if self.on_sample:
                self.on_sample()

    def summary(self) -> dict:
        wall = time.monotonic() - self._t0
        out = {
            "group": self.group, "epoch": self.epoch,
            "wall_seconds": round(wall, 2),
            "executions": self.executions,
            "exec_seconds": round(self.exec_seconds, 2),
            "feedback_seconds": round(self.feedback_seconds, 2),
            "feedback_overhead": (round(self.feedback_seconds /
                                        max(self.exec_seconds, 1e-9), 4)),
            "throughput_exec_s": round(self.executions / max(wall, 1e-9), 1),
            "blocks": self.blocks_now, "edges": self.edges_now,
            "candidates_saved": self.candidates_saved,
        }
        if self.state_atoms_reached is not None:
            out["state_atoms_reached"] = self.state_atoms_reached
        if self.state_injections is not None:
            out["state_injections"] = self.state_injections
        return out

    def close(self) -> None:
        self._snap.close()
        self._cand.close()
