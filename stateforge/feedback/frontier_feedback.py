"""Phase-3.1:Frontier-Directed Structural Feedback。

S(x) = N_B(x) + N_E(x) + 1/(1+D(x)),λ=1 固定(不调参)。
保留准则(3.1 最小版,防 corpus 爆炸):新 BB ∨ 新边 ∨ 全局最优距离刷新
(单冠军槽;per-frontier 冠军与 Progress 属 3.2)。
前沿版本变化时冠军距离重置(前沿消失即冠军退役,历史在日志)。
Phase-1 冻结口径零改动:CS/RS 仅参与距离图,覆盖分母不变。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from stateforge.core.ir import ProgramIR
from stateforge.dynamic.mapper import MappedTrace
from stateforge.feedback.distance_oracle import DistanceOracle
from stateforge.feedback.frontier_portfolio import FrontierPortfolio
from stateforge.feedback.frontier_tracker import FrontierTracker
from stateforge.feedback.structural import StructuralFeedback


@dataclass
class FrontierObservation:
    new_blocks: int = 0
    new_edges: int = 0
    distance: int | None = None            # None = UNREACHABLE
    nearest_frontier: str | None = None
    proximity: float = 0.0
    score: float = 0.0
    interesting: bool = False
    reason: str = "none"                   # new_blocks/new_edges/frontier_proximity
    frontier_version: int = 0
    edge_frontier_count: int = 0


class FrontierDirectedFeedback:
    def __init__(self, ir: ProgramIR):
        self.ir = ir
        self.novelty = StructuralFeedback()
        self.tracker = FrontierTracker(ir)
        self.oracle = DistanceOracle(ir)
        self.portfolio = FrontierPortfolio()       # 3.2:per-frontier 冠军组合
        self._observe_calls = 0

    # ---- 状态持久化(跨 epoch) ----
    def export_state(self) -> dict:
        state = self.tracker.state()
        state["portfolio"] = self.portfolio.state()
        return state

    def load_state(self, state: dict) -> None:
        blocks = set(state.get("covered_blocks", []))
        edges = {tuple(e) for e in state.get("covered_edges", [])}
        self.novelty.update_global_coverage(blocks, edges)   # 修:novelty 全局一并恢复
        self.tracker.covered_blocks, self.tracker.covered_edges = set(blocks), set(edges)
        self.tracker._recompute()
        self.tracker.version = state.get("version", 0) + 1
        self.portfolio.load_state(state.get("portfolio", {}))
        self.oracle.rebuild(self.tracker.edge_frontiers, self.tracker.version)

    # ---- 逐输入评估:先评分(旧覆盖/旧前沿),后 commit ----
    def observe(self, trace: MappedTrace,
                input_id: str = "") -> FrontierObservation:
        """tracker 只在 commit 更新 → 此处的前沿恒为执行前状态。"""
        obs = self.novelty.observe(trace)               # 对执行前快照
        self.oracle.rebuild(self.tracker.edge_frontiers, self.tracker.version)
        d, near = self.oracle.distance(set(trace.block_hits))
        proximity = 1.0 / (1 + d) if d is not None else 0.0
        obs31 = FrontierObservation(
            new_blocks=len(obs.new_blocks), new_edges=len(obs.new_edges),
            distance=d, nearest_frontier=near, proximity=proximity,
            frontier_version=self.tracker.version,
            edge_frontier_count=len(self.tracker.edge_frontiers))
        obs31.score = obs31.new_blocks + obs31.new_edges + proximity

        if obs31.new_blocks or obs31.new_edges:
            obs31.interesting, obs31.reason = True, \
                "new_blocks" if obs31.new_blocks else "new_edges"
        elif near is not None and d is not None:
            kept, reason = self.portfolio.challenge(          # 3.2:组合挑战
                near, d, input_id, self._current_epoch, self.tracker.version)
            if kept:
                obs31.interesting, obs31.reason = True, reason
        return obs31

    _current_epoch = 0

    def commit(self, trace: MappedTrace, kept: bool) -> None:
        """评分完成后并入全局覆盖;前沿版本变化 → 距离表重建+组合退役。"""
        blocks, edges = self.novelty.evidence(trace)
        self.novelty.update_global_coverage(blocks, edges)
        grew = self.tracker.update(blocks, edges)
        if grew:
            self.oracle.rebuild(self.tracker.edge_frontiers,
                                self.tracker.version)
            self.portfolio.retire({f"{u}->{v}"                 # §15:冠军退役
                                   for (u, v) in self.tracker.edge_frontiers})
