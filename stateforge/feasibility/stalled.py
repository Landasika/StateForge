"""Phase-4.2-B:Stalled Guarded Frontier 发现(参数冻结:N=100)。

源块 hit ≥ N ∧ 该边从未被观测 ∧ guard 可恢复 → StalledGuardedFrontier。
N=100 区分"还没试到"(低命中)与"反复到达门前但过不去"(高命中零跨越)。
"""

from __future__ import annotations

from dataclasses import dataclass

from stateforge.core.ir import ProgramIR
from stateforge.feasibility.guard_ir import BranchGuard
from stateforge.feasibility.guard_recovery import _TestIndex, recover_guard

N_MIN_HITS = 100            # 冻结:源块最低命中次数


@dataclass(frozen=True)
class StalledFrontier:
    edge: tuple[str, str]
    source_hit_count: int
    guard: BranchGuard

    @property
    def frontier_id(self) -> str:
        return f"{self.edge[0]}->{self.edge[1]}"


def find_stalled(ir: ProgramIR, covered_blocks: dict[str, int],
                 covered_edges: set[tuple[str, str]],
                 test_index: _TestIndex | None = None,
                 min_hits: int = N_MIN_HITS
                 ) -> list[StalledFrontier]:
    """返回当前全部 stalled guarded frontier(按源块 hit 降序)。"""
    idx = test_index or _TestIndex(ir)
    out: list[StalledFrontier] = []
    for e in ir.edges:
        if e.kind not in ("true", "false"):
            continue
        u, v = e.src, e.dst
        hits = covered_blocks.get(u, 0)
        if hits < min_hits or (u, v) in covered_edges:
            continue
        guard = recover_guard(ir, u, v, e.kind, idx)
        if guard.status != "ok":
            continue
        out.append(StalledFrontier((u, v), hits, guard))
    out.sort(key=lambda sf: -sf.source_hit_count)
    return out
