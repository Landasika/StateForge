"""Phase-4.2:LLM Seed Injector——轮间种子注入(不执行目标)。

架构:LLM 只产种子文件写入 Atheris corpus 目录;执行/变异/覆盖反馈
全部由 Atheris 自己做。本模块不知道目标程序怎么跑。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from stateforge.core.ir import ProgramIR
from stateforge.feasibility.guard_recovery import (_TestIndex,
                                                    recover_guard)
from stateforge.feasibility.enhanced_provenance import resolve_enhanced
from stateforge.feasibility.llm_solver import LLMSemanticSolver
from stateforge.feasibility.proposal import ProposalStatus
from stateforge.feasibility.proposal_validator import validate
from stateforge.fuzz.constraint_mutator import (MutationError,
                                                apply_hint)
from stateforge.fuzz.corpus_manager import CorpusManager
from stateforge.feasibility.mutation_hint import MutationHint


def inject_llm_seeds(ir: ProgramIR, app_path: Path,
                     coverage_state: dict,
                     corpus_dir: Path, provider,
                     max_seeds: int = 3,
                     min_hits: int = 50) -> list[dict]:
    """从覆盖状态找 stalled frontier → LLM → 种子写入 corpus。

    不执行目标;只是产种子文件让下一轮 Atheris 加载变异。
    返回每次注入的审计记录。
    """
    from stateforge.feasibility.semantic_slice import build_slice
    from stateforge.feasibility.stalled import StalledFrontier

    covered_blocks = set(coverage_state.get("covered_blocks", []))
    covered_edges = {tuple(e) for e in
                     coverage_state.get("covered_edges", [])}
    if not covered_blocks:
        return []

    index = _TestIndex(ir)
    solver = LLMSemanticSolver(provider)
    records = []

    # 找 stalled guarded frontiers
    stalled = []
    for e in ir.edges:
        if e.kind not in ("true", "false"):
            continue
        u, v = e.src, e.dst
        if u not in covered_blocks or (u, v) in covered_edges:
            continue
        guard = recover_guard(ir, u, v, e.kind, index)
        if guard.status == "BRANCH_NOT_FOUND":
            continue          # 完全恢复不了才跳;UNSUPPORTED 交给 LLM
        stalled.append(StalledFrontier((u, v), min_hits, guard))
        if len(stalled) >= max_seeds:
            break

    if not stalled:
        return []

    # 从 corpus 取当前最佳种子作为基底
    base_seed = _load_base_seed(corpus_dir)

    for sf in stalled:
        block = ir.blocks[sf.edge[0]]
        prov = resolve_enhanced(ir, app_path, block.function,
                                 sf.guard.lineno)
        slice_ = build_slice(ir, sf, [], prov, app_path)

        try:
            proposal, latency, tokens = solver.solve(slice_)
        except Exception:                          # noqa: BLE001
            records.append({"frontier": sf.frontier_id,
                            "status": "PROVIDER_ERROR"})
            continue

        if proposal.status != ProposalStatus.PROPOSED:
            records.append({"frontier": sf.frontier_id,
                            "status": proposal.status.value})
            continue

        vres = validate(proposal, slice_)
        if not vres.valid:
            records.append({"frontier": sf.frontier_id,
                            "status": f"REJECTED_{vres.rejection_reason}"})
            continue

        # 构造种子并写入 corpus(唯一副作用)
        hint = MutationHint(
            frontier_id=sf.frontier_id, guard_id=sf.guard.guard_id,
            source_kind=proposal.source_kind,
            source_key=proposal.source_key,
            current_value=proposal.proposed_value,
            target_value=proposal.proposed_value,
            operator=sf.guard.op,
            desired_polarity=sf.guard.polarity,
            reason="solved", confidence=proposal.confidence)
        try:
            seed = apply_hint(base_seed, hint)
            seed_bytes = json.dumps(seed, sort_keys=True).encode()
            cid = CorpusManager(corpus_dir.parent / "llm_spool").save(
                seed_bytes, epoch=0, new_blocks=0, new_edges=0,
                discovered_blocks=0, discovered_edges=0,
                nearest_frontier=sf.frontier_id,
                interesting_reason="llm_proposal", origin="LLM")
            if cid:
                (corpus_dir / cid).write_bytes(seed_bytes)
                records.append({"frontier": sf.frontier_id,
                                "status": "INJECTED",
                                "seed_id": cid,
                                "value": str(proposal.proposed_value)[:50]})
            else:
                records.append({"frontier": sf.frontier_id,
                                "status": "DUPLICATE"})
        except MutationError as exc:
            records.append({"frontier": sf.frontier_id,
                            "status": f"MUTATION_ERROR_{exc}"})

    return records


def _load_base_seed(corpus_dir: Path) -> dict:
    """从 Atheris corpus 取一个 dict 种子(跳过 int/str 等非 dict JSON)。"""
    if corpus_dir.exists():
        for f in sorted(corpus_dir.glob("*")):
            if f.is_file():
                try:
                    data = json.loads(f.read_bytes())
                    if isinstance(data, dict):
                        return data
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
    return {"query": {}}
