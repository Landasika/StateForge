"""Phase-4.2 触发组合器:Plateau ∧ StalledFrontier ∧ DeterministicFailure。

本轮只做检测与切片(不接 LLM):满足三条件时输出 trigger=True 并携带
FrontierSemanticSlice——4.2-D 时它就是 LLM 的完整输入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from stateforge.core.ir import ProgramIR
from stateforge.feasibility.plateau import PlateauDetector
from stateforge.feasibility.semantic_slice import build_slice
from stateforge.feasibility.stalled import StalledFrontier, find_stalled


@dataclass
class StagnationTrigger:
    triggered: bool = False
    plateau: str = "RUNNING"
    stalled_count: int = 0
    deterministic_failure: bool = False
    slices: list[dict] = field(default_factory=list)


class StagnationMonitor:
    """组合三条件;每次 fuzz 输入后调用 observe()。"""

    def __init__(self, ir: ProgramIR, app_path: str | Path,
                 window: int = 2000, min_hits: int = 100,
                 max_slices: int = 3):
        self.ir = ir
        self.app_path = Path(app_path)
        self.plateau = PlateauDetector(window)
        self.min_hits = min_hits
        self.max_slices = max_slices          # 触发时最多携带的切片数(冻结)
        # 运行时侧暂存(由 collector/solve_loop 喂入)
        self.observations: list = []          # BranchObservation 累积
        self.corpus_meta: list[dict] = []     # 失败突变记录
        self.deterministic_failure = False    # solve_loop 拒绝时置 True
        self._last_state = StagnationTrigger()

    def observe(self, total_executions: int, covered_blocks: dict[str, int],
                covered_edges: set, total_crossings: int) -> StagnationTrigger:
        status = self.plateau.observe(total_executions,
                                      len(covered_blocks),
                                      len(covered_edges),
                                      total_crossings)
        stalled = find_stalled(self.ir, covered_blocks, covered_edges,
                               min_hits=self.min_hits)
        self._last_state = StagnationTrigger(
            triggered=(status == "STAGNATED" and bool(stalled)
                       and self.deterministic_failure),
            plateau=status,
            stalled_count=len(stalled),
            deterministic_failure=self.deterministic_failure)
        if self._last_state.triggered:
            self._build_slices(stalled[:self.max_slices])
        return self._last_state

    def _build_slices(self, stalled: list[StalledFrontier]) -> None:
        from stateforge.feasibility.input_provenance import (
            resolve_operand_source)
        self._last_state.slices = []
        for sf in stalled:
            block = self.ir.blocks[sf.edge[0]]
            prov = resolve_operand_source(self.app_path, block.function,
                                          sf.guard.lineno)
            self._last_state.slices.append(
                build_slice(self.ir, sf, self.observations, prov,
                            self.app_path, self.corpus_meta))
