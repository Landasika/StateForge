"""Phase-3.2:Per-Frontier Champion Portfolio + Progress 事件。

顾问修订版 3.2 核心:
- champion 的目的是**压缩语料**(每活跃前沿 ≤1 冠军,K=|活跃前沿|),对抗稀释
- Progress 是事件(同前沿上 D(x,f) < D_best(f) → 换冠军),不混入总分
- 保留理由三种:STRUCTURAL_NOVELTY / FRONTIER_CHAMPION(首任)/ FRONTIER_PROGRESS(替换)

近似声明(写入论文限制):冠军只在输入的**最近前沿**上发起挑战——输入的
变异杠杆天然朝其最近前沿;全前沿距离需 |F| 张 BFS 表,3.2 不做。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ChampionRecord:
    input_id: str
    distance: int
    epoch: int
    frontier_version: int


@dataclass
class PortfolioEvent:
    kind: str                       # champion_new / champion_replace / champion_retire
    frontier: str
    input_id: str | None = None
    old_distance: int | None = None
    new_distance: int | None = None
    epoch: int = 0


class FrontierPortfolio:
    def __init__(self) -> None:
        self.champions: dict[str, ChampionRecord] = {}
        self.events: list[PortfolioEvent] = []
        self.replacement_count = 0

    # ---- 挑战:输入对其最近前沿 ----
    def challenge(self, frontier: str, distance: int, input_id: str,
                  epoch: int, version: int) -> tuple[bool, str]:
        """返回 (是否保留, 理由 FRONTIER_CHAMPION/FRONTIER_PROGRESS/否)。"""
        cur = self.champions.get(frontier)
        if cur is None:
            self.champions[frontier] = ChampionRecord(input_id, distance,
                                                      epoch, version)
            self.events.append(PortfolioEvent("champion_new", frontier,
                                              input_id, None, distance, epoch))
            return True, "frontier_champion"
        if distance < cur.distance:
            self.champions[frontier] = ChampionRecord(input_id, distance,
                                                      epoch, version)
            self.replacement_count += 1
            self.events.append(PortfolioEvent(
                "champion_replace", frontier, input_id,
                cur.distance, distance, epoch))
            return True, "frontier_progress"
        return False, "none"

    # ---- 前沿版本更替:退役已消失前沿的冠军 ----
    def retire(self, active_frontiers: set[str]) -> None:
        for f in list(self.champions):
            if f not in active_frontiers:
                rec = self.champions.pop(f)
                self.events.append(PortfolioEvent(
                    "champion_retire", f, rec.input_id, rec.distance,
                    None, rec.epoch))

    def drain_events(self) -> list[PortfolioEvent]:
        out, self.events = self.events, []
        return out

    def state(self) -> dict:
        return {"champions": {f: vars(r) for f, r in self.champions.items()},
                "replacement_count": self.replacement_count}

    def load_state(self, state: dict) -> None:
        self.champions = {f: ChampionRecord(**r)
                          for f, r in state.get("champions", {}).items()}
        self.replacement_count = state.get("replacement_count", 0)
