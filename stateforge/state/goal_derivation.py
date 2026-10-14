"""goal_derivation.py —— Phase 2(MVP):目标基本块 → 状态约束(StateGoal)。

路线(顾问 Phase-2 口径):给定未覆盖目标块,在 ICFG 上找其**必要分支边**
(删边后入口不可达目标的 true/false 边 = 控制依赖守卫),再把守卫块的
行号区间匹配到 StateFact 的 CONTROL 读点,收集其结构化谓词,构成
StateGoal.constraints——"要覆盖此块,持久状态须满足这些条件"。

诚实边界(MVP):
- 必要边 = 函数内支配近似(删边可达性),不做过程间;
- 谓词来自 extract_state_facts 的词面(容器 dict 库/Session/ORM),
  词面外的守卫不产约束(与 5A 词汇边界一致);
- EQ 谓词值侧为动态变量时 value=None(主体侧信息保留,Phase-3 规划期
  经绑定上下文补全)。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from stateforge.core.ir import ProgramIR
from stateforge.state.model import StateFactCandidate, StructuredPredicate


@dataclass(frozen=True)
class StateGoal:
    """覆盖目标块所需的持久状态约束集。"""

    target_block: str
    function: str
    file: str
    constraints: tuple[StructuredPredicate, ...]
    guard_blocks: tuple[str, ...] = field(default=())
    evidence: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict:
        return {
            "target_block": self.target_block,
            "function": self.function,
            "file": self.file,
            "constraints": [p.to_dict() for p in self.constraints],
            "guard_blocks": list(self.guard_blocks),
            "evidence": list(self.evidence),
        }


def _function_blocks(ir: ProgramIR, block_id: str) -> set[str]:
    """目标块所在函数的块集(按 file+function 归属)。"""
    blk = ir.blocks.get(block_id)
    if blk is None:
        return set()
    return {b.id for b in ir.blocks.values()
            if b.file == blk.file and b.function == blk.function}


def _reachable(src: str, dst: str, block_ids: set[str], ir: ProgramIR,
               skip_edge: tuple[str, str] | None = None) -> bool:
    """函数子图内 src→dst 可达性(可删一条边)。"""
    adj: dict[str, list[str]] = {}
    for e in ir.edges:
        if e.src not in block_ids or e.dst not in block_ids:
            continue
        if skip_edge and (e.src, e.dst) == skip_edge:
            continue
        adj.setdefault(e.src, []).append(e.dst)
    seen = {src}
    stack = [src]
    while stack:
        n = stack.pop()
        if n == dst:
            return True
        for m in adj.get(n, []):
            if m not in seen:
                seen.add(m)
                stack.append(m)
    return False


def _entry_of(ir: ProgramIR, block_ids: set[str]) -> str | None:
    """函数子图入口(无入边的块;多入口取行号最小者)。"""
    has_in = {e.dst for e in ir.edges
              if e.src in block_ids and e.dst in block_ids}
    entries = [b for b in block_ids if b not in has_in]
    if not entries:
        return None
    return min(entries, key=lambda b: ir.blocks[b].start_line)


def necessary_guard_edges(ir: ProgramIR,
                          target_block: str) -> list[tuple[str, str]]:
    """支配目标块的 (分支块, 必要边方向)——方向感知版。

    false 边 = 目标在守卫的否定侧(如越权分支 owner≠uid),
    是谓词反转(EQ→NE)的依据。
    """
    fblocks = _function_blocks(ir, target_block)
    entry = _entry_of(ir, fblocks)
    if entry is None or entry == target_block:
        return []
    out: list[tuple[str, str]] = []
    branch_blocks = [b for b in fblocks
                     if any(e.src == b and e.kind in ("true", "false")
                            for e in ir.edges)]
    for b in branch_blocks:
        for e in ir.edges:
            if e.src != b or e.kind not in ("true", "false"):
                continue
            if not _reachable(entry, target_block, fblocks, ir,
                              skip_edge=(e.src, e.dst)):
                if (b, e.kind) not in out:
                    out.append((b, e.kind))
                break
    return sorted(out, key=lambda t: ir.blocks[t[0]].start_line)


def necessary_guards(ir: ProgramIR, target_block: str) -> list[str]:
    """支配目标块的分支块:删其任一出边后入口不可达目标。"""
    return [b for b, _kind in necessary_guard_edges(ir, target_block)]


def _same_file(a: str, b: str) -> bool:
    """文件路径形态归一(icfg 产绝对路径,extractor 可能为相对路径)。"""
    from pathlib import Path

    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return a == b


def _facts_in_block(facts: list[StateFactCandidate], file: str,
                    start: int, end: int) -> list[StateFactCandidate]:
    return [f for f in facts
            if _same_file(f.source_file, file) and start <= f.lineno <= end
            and f.relevance.value == "CONTROL"
            and f.structured_predicate is not None]


def derive_state_goal(ir: ProgramIR, facts: list[StateFactCandidate],
                      target_block: str) -> StateGoal | None:
    """目标块 → StateGoal(必要守卫上的 CONTROL 状态读点谓词集)。

    方向感知(否定谓词):目标经守卫 **false 边**到达时,该守卫的
    EQ/IN 谓词反转(NE/NOT_IN)——越权分支(owner≠uid)的约束来源。
    EXISTS 不反转(None-检查/真值的语义已内含方向,边界见文档)。
    """
    blk = ir.blocks.get(target_block)
    if blk is None:
        return None
    from stateforge.state.model import PredicateOp, StructuredPredicate

    _invert = {"EQ": PredicateOp.NE, "IN": PredicateOp.NOT_IN}
    guards = necessary_guard_edges(ir, target_block)
    constraints: list = []
    evidence: list[str] = []
    for g, edge_kind in guards:
        gblk = ir.blocks[g]
        for f in _facts_in_block(facts, gblk.file,
                                 gblk.start_line, gblk.end_line):
            pred = f.structured_predicate
            if edge_kind == "false" and pred.operator.value in _invert:
                pred = StructuredPredicate(
                    _invert[pred.operator.value], pred.subject, pred.value)
            if pred in constraints:
                continue                      # 同谓词去重(EXISTS 双源)
            constraints.append(pred)
            evidence.append(
                f"{f.source_file}:{f.lineno} {f.object_ref}"
                f" {pred.operator.value}"
                f"{'' if edge_kind == 'true' else '(false 边反转)'}")
    # 目标块自身的 CONTROL 读点(块内守卫)也入约束
    for f in _facts_in_block(facts, blk.file, blk.start_line, blk.end_line):
        if f.structured_predicate in constraints:
            continue
        constraints.append(f.structured_predicate)
        evidence.append(
            f"{f.source_file}:{f.lineno} {f.object_ref}"
            f" {f.structured_predicate.operator.value}")
    return StateGoal(
        target_block=target_block,
        function=blk.function,
        file=blk.file,
        constraints=tuple(constraints),
        guard_blocks=tuple(b for b, _kind in guards),
        evidence=tuple(evidence),
    )
