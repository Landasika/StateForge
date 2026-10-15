"""goal_seed_injector.py —— F 臂注入器:未覆盖深块(目标块直导)→ 种子。

契约与 inject_state_seeds 逐参相同(epoch P4-T4 注入接缝):
    (ir, target, state, corpus_dir, budget=STATE_BUDGET) → 写入数

差异(唯一):目标来源。E 臂按 M1 状态原子词汇找未达原子;本注入器
按**运行时覆盖代理中未覆盖的深块**直接目标块推导(goal_to_plan),
即 Phase 1-4 验证的"目标块→约束→链"管线,链经 goal_checkpoint 落
C10 序列种子(goal_spool 惯例),由 Atheris 下一轮加载执行并变异。
"""

from __future__ import annotations

from pathlib import Path

from stateforge.solving.state_seed_injector import STATE_BUDGET


def _deep_block_candidates(ir, target: Path,
                           covered: set[str]) -> list[str]:
    """未覆盖深块:目标文件 _handle 内、kind=block、含调用语句。

    与 goal_directed_eval.derive_state_gated_blocks 同判定,但目标集
    来自战役运行时覆盖代理(global_state.json 的 blocks),非冷启动扫描。
    """
    src_name = target.name
    out = []
    for b in ir.blocks.values():
        if (src_name in b.file and b.function == "_handle"
                and b.kind == "block" and b.id not in covered
                and any("(" in s and not s.startswith("HITS")
                        for s in b.statements)):
            out.append(b.id)
    return sorted(out, key=lambda bid: ir.blocks[bid].start_line)


def inject_goal_seeds(ir, target, state, corpus_dir,
                      budget: int = STATE_BUDGET) -> int:
    """F 臂:未覆盖深块 → plan_for_block → C10 种子写语料;返回写入数。"""
    from stateforge.solving.goal_checkpoint import (GoalCheckpoint,
                                                    inject_checkpoint_seed)
    from stateforge.state.extractor import extract_state_facts
    from stateforge.state.goal_to_plan import plan_for_block

    covered = set(state.get("blocks", []))
    candidates = _deep_block_candidates(ir, Path(target), covered)
    if not candidates:
        return 0
    facts = extract_state_facts(Path(target).parent, ir=ir)
    written = 0
    for bid in candidates:
        if written >= budget:
            break
        planned = plan_for_block(ir, facts, bid)
        if planned is None:
            continue
        goal, segment = planned
        ckpt = GoalCheckpoint(
            checkpoint_id=f"fs-{abs(hash(bid)) % 10**10}",
            target_block=bid, source_file=str(target),
            goal_constraints=[p.to_dict() for p in goal.constraints],
            steps=[{"json": dict(s.json)} for s in segment.steps],
            hits=[],
            step_evidence=[{"action": s.action} for s in segment.steps])
        if inject_checkpoint_seed(ckpt, Path(corpus_dir)):
            written += 1
    return written
