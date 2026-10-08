"""Phase-4.2-A:Coverage Plateau Detector(参数实验前冻结:W=2000)。

滑动窗口看三差:ΔBB / ΔEdge / ΔFrontierCrossing 全零 → STAGNATED。
滞回:一旦 STAGNATED,只有 frontier crossing 发生才回 RUNNING
(单次新块/新边不够,防噪声抖动)。
"""

from __future__ import annotations

from dataclasses import dataclass, field

W_WINDOW = 2000            # 冻结:窗口(执行次数,非墙钟——跨解释器可比)


@dataclass
class PlateauState:
    status: str = "RUNNING"             # RUNNING / STAGNATED
    window_start_exec: int = 0
    window_start_blocks: int = 0
    window_start_edges: int = 0
    window_start_crossings: int = 0
    stagnated_since: int | None = None  # 进入 STAGNATED 时的总执行数


class PlateauDetector:
    """逐输入喂 observe();每次返回当前状态。"""

    def __init__(self, window: int = W_WINDOW):
        self.window = window
        self._state = PlateauState()

    @property
    def status(self) -> str:
        return self._state.status

    def observe(self, total_executions: int, total_blocks: int,
                total_edges: int, total_crossings: int) -> str:
        s = self._state
        if s.status == "RUNNING":
            in_window = total_executions - s.window_start_exec
            if in_window >= self.window:
                d_blocks = total_blocks - s.window_start_blocks
                d_edges = total_edges - s.window_start_edges
                d_cross = total_crossings - s.window_start_crossings
                if d_blocks == 0 and d_edges == 0 and d_cross == 0:
                    s.status = "STAGNATED"
                    s.stagnated_since = total_executions
                else:
                    # 滑窗推进:以本次为起点重新观察一个窗口
                    s.window_start_exec = total_executions
                    s.window_start_blocks = total_blocks
                    s.window_start_edges = total_edges
                    s.window_start_crossings = total_crossings
        elif s.status == "STAGNATED":
            # 滞回:仅 frontier crossing 解除平台
            if total_crossings > s.window_start_crossings:
                s.status = "RUNNING"
                s.window_start_exec = total_executions
                s.window_start_blocks = total_blocks
                s.window_start_edges = total_edges
                s.window_start_crossings = total_crossings
                s.stagnated_since = None
        return s.status
