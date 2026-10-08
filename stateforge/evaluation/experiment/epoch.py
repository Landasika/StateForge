"""Phase-2 模块 4(评审修订版):epoch 控制 + 公平性记账。

P0-4:每 epoch 记录子进程真实起止(campaign 相对墙钟),不再假装无重启间隙
P0-5:executions/exec_seconds/feedback_seconds/candidates 跨 epoch 累计
P0-8:每 epoch 记录语料 size/bytes(输入侧与 libFuzzer 终态)
P0-9:NovelSeedGain——SF 候选中 native 语料没有的比例(信息增益)
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from stateforge.fuzzing.corpus_manager import CorpusManager

EXECUTION_EDGE_KINDS = {"flow", "true", "false", "call", "return"}


def _dir_stats(d: Path) -> dict:
    files = [f for f in d.iterdir() if f.is_file()] if d.exists() else []
    return {"files": len(files), "bytes": sum(f.stat().st_size for f in files)}


def _run_driver(target: str, ir: str, corpus_dir: Path, out_dir: Path,
                seconds: float, mode: str, seed: int, group: str,
                epoch: int) -> None:
    cmd = [sys.executable, "-m", "stateforge.fuzzing.entry_driver",
           target, ir, str(corpus_dir), str(out_dir),
           "--seconds", str(seconds), "--mode", mode,
           "--seed", str(seed), "--epoch", str(epoch), "--group", group]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=seconds * 4 + 120)
    if proc.returncode not in (0, -2, 245):
        raise RuntimeError(f"driver 失败 rc={proc.returncode}: "
                           f"{proc.stderr[-2000:]}")


def _load_epoch_summary(out_dir: Path, group: str, epoch: int) -> dict:
    p = out_dir / f"{group}-e{epoch}-snapshots.jsonl"
    snaps = [json.loads(line) for line in
             p.read_text(encoding="utf-8").splitlines() if line.strip()]
    cand_p = out_dir / f"{group}-e{epoch}-candidates.jsonl"
    cands = [json.loads(line) for line in
             cand_p.read_text(encoding="utf-8").splitlines() if line.strip()] \
        if cand_p.exists() else []
    last = snaps[-1] if snaps else {}
    return {"epoch": epoch, "snapshots": snaps,
            "executions": last.get("executions", 0),
            "blocks": last.get("blocks", 0), "edges": last.get("edges", 0),
            "exec_seconds": last.get("exec_seconds", 0.0),
            "feedback_seconds": last.get("feedback_seconds", 0.0),
            "candidates_saved": len(cands)}


def _novel_seed_gain(workdir: Path) -> dict:
    """P0-9:SF 候选 ∩ native(libFuzzer)语料 → 真正额外种子比例。"""
    spool = workdir / "epochs" / "spool" / "seeds"
    lf = workdir / "lf_corpus"
    sf_ids = {f.name for f in spool.iterdir()} if spool.exists() else set()
    lf_ids = {CorpusManager.content_id(f.read_bytes())
              for f in lf.iterdir() if f.is_file()} if lf.exists() else set()
    total, overlap = len(sf_ids), len(sf_ids & lf_ids)
    return {"sf_candidates_total": total,
            "sf_already_in_native": overlap,
            "sf_unique_vs_native": total - overlap,
            "novel_seed_gain": round((total - overlap) / total, 4) if total else None}


def _inject_llm_between_epochs(ir, workdir: Path, corpus_dir: Path,
                               target: str,
                               stall_tracker=None, epoch_num: int = 0,
                               prev_blocks: int = 0, prev_edges: int = 0) -> list[dict]:
    """轮间 LLM 种子注入(不执行目标,只产种子写 corpus)。

    集成 StallTracker:只在 frontier 真正停滞(stall_window 轮无改善)
    时触发 LLM,不是每轮都调。
    prev_blocks/prev_edges = 上上轮末的累计覆盖(run_epochs 滞后一轮滚存,
    I4 修复):new_blocks/new_edges 即刚结束那轮的真增量,有进展重置、
    无进展累计停滞。
    """
    import json as _json
    from stateforge.solving.llm_seed_injector import inject_llm_seeds

    state_path = workdir / "epochs" / "global_state.json"
    if not state_path.exists():
        return []
    state = _json.loads(state_path.read_text(encoding="utf-8"))

    # StallTracker 判定:这个 epoch 是否有真正的停滞
    active_frontiers = {tuple(e) for e in state.get("edge_frontiers", [])
                        if len(e) == 2}
    cur_blocks = len(state.get("covered_blocks", []))
    cur_edges = len(state.get("covered_edges", []))
    new_blocks = cur_blocks - prev_blocks
    new_edges = cur_edges - prev_edges

    if stall_tracker is not None:
        to_inject = stall_tracker.observe_epoch(
            epoch_num, active_frontiers, new_blocks, new_edges)
        if not to_inject:
            return []                        # 没有停滞 frontier → 不调 LLM

    provider_path = Path(target).parent / (Path(target).stem + ".llm.py")
    if not provider_path.exists():
        return []                            # 无 provider → 跳过
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location("llm_provider", provider_path)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    provider = mod.provider

    from stateforge.core.ir import ProgramIR
    ir_obj = ProgramIR.from_json(ir)
    records = inject_llm_seeds(ir_obj, Path(target), state,
                               corpus_dir, provider)
    if records and stall_tracker is not None:
        for r in records:
            edge = tuple(r.get("frontier", "->").split("->"))
            stall_tracker.mark_injected(edge)
    if records:
        llm_log = workdir / "epochs" / "llm_injections.jsonl"
        with llm_log.open("a", encoding="utf-8") as fh:
            for r in records:
                fh.write(_json.dumps(r, ensure_ascii=False) + "\n")
    return records


def _inject_state_seeds_between_epochs(ir, workdir: Path, corpus_dir: Path,
                                        target: str,
                                        epoch_num: int = 0,
                                        trigger: str = "r2",
                                        injector=None) -> int:
    """轮间 state 序列种子注入(P3-T3,policy="state" 专用;零 LLM)。

    R2 触发(与 llm 臂的 StallTracker 判停同形、状态粒度):
      (a) 未达深状态原子存在——injector 内部 goal 派生(全部 requires
          原子 − 已达原子),无 goal 即零写入;
      (b) 上一轮无新状态节点——`epochs/state_ledger.json` 差分
          (本函数每轮先记账再判定;不用 PlateauDetector)。
    状态节点记账 = 覆盖代理(守卫真分支块已覆盖 ⇒ 原子判达):
    entry_driver 不产会话观测,补观测即膨胀驱动(裁决禁止);机制与
    诚实边界见 solving/state_seed_injector.py 模块 docstring §3。

    P4-T4 消融接缝(可选参,缺省 = E 基线行为逐字节不变):
      trigger="always" —— E-always 臂:旁路 R2-(b)(有进展也注入),
          R2-(a) 仍由 injector 的 goal 派生保留;旁路时审计
          INJECT_ALWAYS(带 new_atoms,与 SKIP 同形可查)。
      injector=callable —— E-rand 臂:注入策略钩子,以与
          inject_state_seeds 同一契约 (ir, target, state, corpus_dir,
          budget=STATE_BUDGET) 调用、返回写入数。
    """
    import json as _json
    from stateforge.core.ir import ProgramIR
    from stateforge.solving.state_seed_injector import (STATE_BUDGET,
                                                        inject_state_seeds,
                                                        reached_state_atoms)

    state_path = workdir / "epochs" / "global_state.json"
    if not state_path.exists():
        return 0                                # mode=off 等无覆盖态 → 跳过
    state = _json.loads(state_path.read_text(encoding="utf-8"))
    ir_obj = ProgramIR.from_json(ir)

    ledger_path = workdir / "epochs" / "state_ledger.json"
    prior: set = set()
    if ledger_path.exists():
        prior = set(_json.loads(
            ledger_path.read_text(encoding="utf-8")).get("atoms", []))
    now = reached_state_atoms(ir_obj, Path(target), state)
    ledger_path.write_text(
        _json.dumps({"epoch": epoch_num, "atoms": sorted(now)}),
        encoding="utf-8")

    new_atoms = sorted(now - prior)
    if new_atoms:
        if trigger == "always":                 # P4-T4 E-always:旁路 R2-(b)
            _state_audit(workdir, {"epoch": epoch_num,
                                   "status": "INJECT_ALWAYS",
                                   "new_atoms": new_atoms})
        else:                                   # R2(b):上轮有新状态节点
            _state_audit(workdir, {"epoch": epoch_num,
                                   "status": "SKIP_R2_PROGRESS",
                                   "new_atoms": new_atoms})
            return 0
    inj = inject_state_seeds if injector is None else injector
    return inj(ir_obj, Path(target), state, corpus_dir, budget=STATE_BUDGET)


def _state_audit(workdir: Path, record: dict) -> None:
    path = workdir / "epochs" / "state_injections.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _llm_audit(workdir: Path, record: dict) -> None:
    """llm 接缝审计(P5 终清扫):同 _state_audit 形状,落 llm 台账。

    接缝异常先记 SEAM_ERROR 再照常跑下一轮(语义与修前 pass 一致,
    只补审计痕,不改控制流)。
    """
    path = workdir / "epochs" / "llm_injections.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _state_boundary(workdir: Path, epoch: int, injected: int) -> dict:
    """P3-T4:轮间状态摘要(演进级)——进 epoch 记录的 rec["state"]。

    数据源 = state 分支刚落盘的产物,不重算:state_ledger.json 的 atoms
    即本边界覆盖代理判达的原子数;state_injections.jsonl 末行为本边界的
    SKIP_R2_PROGRESS 记账时带 new_atoms。
    """
    out: dict = {"epoch": epoch, "atoms_reached": None, "injected": injected}
    ledger = workdir / "epochs" / "state_ledger.json"
    if ledger.exists():
        out["atoms_reached"] = len(json.loads(
            ledger.read_text(encoding="utf-8")).get("atoms", []))
    audit = workdir / "epochs" / "state_injections.jsonl"
    if audit.exists():
        lines = audit.read_text(encoding="utf-8").splitlines()
        if lines:
            last = json.loads(lines[-1])
            if last.get("epoch") == epoch \
                    and last.get("status") == "SKIP_R2_PROGRESS":
                out["status"] = "SKIP_R2_PROGRESS"
                out["new_atoms"] = last.get("new_atoms", [])
    return out


def _final_state_atoms(ir, target: str, workdir: Path) -> set | None:
    """P3-T4:末轮覆盖判达的状态原子(同 injector 机制,报告同源)。

    指标失败不掩盖执行结果:任何异常 → None(字段缺省)。
    """
    state_path = workdir / "epochs" / "global_state.json"
    if not state_path.exists():
        return None
    try:
        from stateforge.core.ir import ProgramIR
        from stateforge.solving.state_seed_injector import reached_state_atoms
        return reached_state_atoms(
            ProgramIR.from_json(ir), Path(target),
            json.loads(state_path.read_text(encoding="utf-8")))
    except Exception:                          # noqa: BLE001
        return None


def run_epochs(target: str, ir: str, workdir: Path, seconds_per_epoch: float,
               epochs: int, seed: int, group: str, mode: str,
               use_feedback: bool, t0: float | None = None,
               policy: str = "reinjection", state_trigger: str = "r2",
               state_injector=None) -> dict:
    """跑一组(单 epoch 连续或多 epoch 重启),返回含墙钟与累计的完整记录。

    policy:reinjection(默认)=公共语料联合结转;llm/state=轮间种子注入
    (注入先于结转,注入种子经公共路径进下一轮);portfolio=P4-T1 起与
    reinjection 等价——原 D2-P focused 语义(偶数轮 focused_in)自 a0cc5bf
    起因分支嵌进 policy==llm 门内而不可达,已随 _focused_corpus 一并删除
    (R6:公平优先于历史连续)。

    state_trigger / state_injector(P4-T4 消融接缝,仅 policy="state" 消费,
    缺省 = E 基线行为逐字节不变):见
    _inject_state_seeds_between_epochs docstring(trigger="r2"|"always";
    injector=注入策略钩子,E-rand 的随机采样器经此接入)。
    """
    t0 = t0 if t0 is not None else time.monotonic()
    workdir.mkdir(parents=True, exist_ok=True)
    corpus_dir = workdir / "lf_corpus"
    corpus_dir.mkdir(exist_ok=True)

    # StallTracker:policy=llm 时追踪前沿停滞,N 轮无改善才触发
    stall_tracker = None
    if policy == "llm":
        from stateforge.solving.stall_tracker import StallTracker
        stall_tracker = StallTracker(stall_window=2, max_llm_per_frontier=3)

    prev_blocks = prev_edges = 0        # 判停差分基线(滞后一轮,见循环尾注释)
    last_blocks = last_edges = 0        # 本轮记录(下一轮变为差分基线)
    epoch_records = []
    for epoch in range(1, epochs + 1):
        in_dir = corpus_dir if epoch == 1 else workdir / f"corp_in_e{epoch}"
        if epoch > 1:
            in_dir.mkdir(parents=True, exist_ok=True)

        # ---- policy 专属轮间注入(先于结转:注入写 corpus_dir,才能被下方
        # 公共结转带入下一轮;llm/state 互斥,各臂至多走其一)
        boundary = None
        if policy == "llm" and epoch > 1:
            try:
                _inject_llm_between_epochs(
                    ir, workdir, corpus_dir, target,
                    stall_tracker=stall_tracker, epoch_num=epoch,
                    prev_blocks=prev_blocks, prev_edges=prev_edges)
            except Exception as exc:       # noqa: BLE001
                _llm_audit(workdir, {"epoch": epoch, "event": "SEAM_ERROR",
                                     "error": str(exc)})  # 失败留审计痕,照常跑
        if policy == "state" and epoch > 1:
            try:
                n_inj = _inject_state_seeds_between_epochs(
                    ir, workdir, corpus_dir, target, epoch_num=epoch,
                    trigger=state_trigger, injector=state_injector)
                boundary = _state_boundary(workdir, epoch, n_inj)
            except Exception as exc:        # noqa: BLE001
                _state_audit(workdir, {"epoch": epoch, "event": "SEAM_ERROR",
                                       "error": str(exc)})  # 失败留审计痕,照常跑
                boundary = {"epoch": epoch, "atoms_reached": None,
                            "injected": 0, "status": "SEAM_ERROR",
                            "error": str(exc)}

        # ---- 轮间语料结转(R6 统一,公共路径):P4-T1 起 B/C/D/E 多轮臂
        # 同一语义——feedback 开 = spool∪corpus,否则仅 corpus(含本轮注入
        # 种子)。修复 a0cc5bf 引入的混淆因子:结转原先在 policy==llm 门内,
        # B/C 自 epoch 2 起空 corp_in 起跑。A 单 epoch 不经此路径。
        if epoch > 1:
            if use_feedback:
                CorpusManager(workdir / "epochs" / "spool").export_seed_dir(
                    in_dir, extra=[corpus_dir])
            else:
                for f in corpus_dir.iterdir():
                    if f.is_file():
                        shutil.copy2(f, in_dir / f.name)

        in_stats = _dir_stats(in_dir)
        start_off = time.monotonic() - t0
        _run_driver(target, ir, in_dir, workdir / "epochs",
                    seconds_per_epoch, mode, seed, group, epoch)
        end_off = time.monotonic() - t0
        if in_dir != corpus_dir:
            for f in in_dir.iterdir():
                if f.is_file():
                    shutil.copy2(f, corpus_dir / f.name)
        rec = _load_epoch_summary(workdir / "epochs", group, epoch)
        rec["wall"] = {"start_offset": round(start_off, 3),
                       "end_offset": round(end_off, 3),
                       "subprocess_seconds": round(end_off - start_off, 3)}
        rec["corpus_in"] = in_stats
        rec["corpus_lf_after"] = _dir_stats(corpus_dir)
        if boundary is not None:              # P3-T4:演进级状态摘要进 epoch 记录
            rec["state"] = boundary
        epoch_records.append(rec)
        # I4(v0.5 后修复):llm 判停差分基线逐轮前移。边界 N 读到的
        # global_state 是第 N−1 轮末的累计覆盖,基线必须是第 N−2 轮末的
        # 记录,new_blocks 才是第 N−1 轮的真增量——故基线滞后一轮
        # (prev←上轮记录,再滚存本轮)。修前基线恒 0 → new=累计值 →
        # StallTracker 永远见"有进展",llm 臂从未触发过注入;若不滞后
        # (prev 直取本轮记录),边界上 cur−prev 恒 0,判停退化为
        # "前沿在场必停滞",同样背离设计(有进展应重置)。
        prev_blocks, prev_edges = last_blocks, last_edges
        last_blocks, last_edges = rec["blocks"], rec["edges"]
    # P0-5:跨 epoch 累计
    totals = {k: sum(r[k] for r in epoch_records) for k in
              ("executions", "exec_seconds", "feedback_seconds",
               "candidates_saved")}
    last = epoch_records[-1]
    result = {"group": group, "mode": mode, "epochs": epochs,
              "seed": seed, "epoch_records": epoch_records,
              "total_executions": totals["executions"],
              "total_exec_seconds": round(totals["exec_seconds"], 3),
              "total_feedback_seconds": round(totals["feedback_seconds"], 3),
              "total_candidates_saved": totals["candidates_saved"],
              "final_blocks": last["blocks"], "final_edges": last["edges"],
              "group_start_offset": round(epoch_records[0]["wall"]["start_offset"], 3),
              "wall_span": round(epoch_records[-1]["wall"]["end_offset"]
                                 - epoch_records[0]["wall"]["start_offset"], 3)}
    if use_feedback and stall_tracker is not None:
        result["stall_summary"] = stall_tracker.summary()
    if use_feedback:
        result["novel_seed_gain"] = _novel_seed_gain(workdir)
        # 3.2 诊断:组合规模/替换数(portfolio_yield 已随 _focused_corpus
        # 死分支删除——focused/normal 轮不再有语义区分,P4-T1)
        state_p = workdir / "epochs" / "global_state.json"
        if state_p.exists():
            pf = __import__("json").loads(
                state_p.read_text(encoding="utf-8")).get("portfolio", {})
            result["portfolio_champions"] = len(pf.get("champions", {}))
            result["portfolio_replacements"] = pf.get("replacement_count", 0)
        last_in = epoch_records[-1].get("corpus_in", {})
        lf_files = _dir_stats(corpus_dir)["files"]
        result["corpus_dilution_ratio"] = round(
            max(last_in.get("files", 0) - lf_files, 0) /
            max(last_in.get("files", 0), 1), 3)
    if policy == "state":                     # P3-T4:状态三指标汇总口径
        result["state_injections_total"] = sum(
            r.get("state", {}).get("injected", 0) for r in epoch_records)
        final = _final_state_atoms(ir, target, workdir)
        if final is not None:
            result["state_atoms_reached"] = len(final)
            result["state_atoms"] = sorted(final)
    (workdir / "group_summary.json").write_text(
        json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    return result
