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


def _focused_corpus(workdir: Path, out_dir: Path) -> Path:
    """D2-P focused epoch 语料:当前 per-frontier 冠军 + 最近 8 个新结构种子。

    外部 epoch 级预算分配(顾问 §4/§5):不碰 Atheris 调度器,只决定本 epoch
    的起始语料 = 把 wall-clock 预算直接交给 StateForge 选出的种子。
    """
    import json as _json
    out = workdir / "focused_in"
    out.mkdir(exist_ok=True)
    for f in out.iterdir():
        f.unlink()
    spool = out_dir / "spool"
    state_p = out_dir / "global_state.json"
    champs: dict[str, str] = {}          # frontier -> input_id
    if state_p.exists():
        champs = {f: r["input_id"] for f, r in
                  _json.loads(state_p.read_text(encoding="utf-8"))
                  .get("portfolio", {}).get("champions", {}).items()}
    seeds = spool / "seeds"
    novel_recent: list[str] = []
    if (spool / "meta.jsonl").exists():
        for line in (spool / "meta.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                m = _json.loads(line)
                if m["interesting_reason"] in ("new_blocks", "new_edges"):
                    novel_recent.append(m["input_id"])
    want = set(champs.values()) | set(novel_recent[-8:])
    for cid in want:
        seed_file = seeds / cid
        if seed_file.exists():
            (out / cid).write_bytes(seed_file.read_bytes())
    return out


def _inject_llm_between_epochs(ir, workdir: Path, corpus_dir: Path,
                               target: str,
                               stall_tracker=None, epoch_num: int = 0,
                               prev_blocks: int = 0, prev_edges: int = 0) -> list[dict]:
    """轮间 LLM 种子注入(不执行目标,只产种子写 corpus)。

    集成 StallTracker:只在 frontier 真正停滞(stall_window 轮无改善)
    时触发 LLM,不是每轮都调。
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
                                        epoch_num: int = 0) -> int:
    """轮间 state 序列种子注入(P3-T3,policy="state" 专用;零 LLM)。

    R2 触发(与 llm 臂的 StallTracker 判停同形、状态粒度):
      (a) 未达深状态原子存在——injector 内部 goal 派生(全部 requires
          原子 − 已达原子),无 goal 即零写入;
      (b) 上一轮无新状态节点——`epochs/state_ledger.json` 差分
          (本函数每轮先记账再判定;不用 PlateauDetector)。
    状态节点记账 = 覆盖代理(守卫真分支块已覆盖 ⇒ 原子判达):
    entry_driver 不产会话观测,补观测即膨胀驱动(裁决禁止);机制与
    诚实边界见 solving/state_seed_injector.py 模块 docstring §3。
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

    if now - prior:                             # R2(b):上轮有新状态节点
        _state_audit(workdir, {"epoch": epoch_num,
                               "status": "SKIP_R2_PROGRESS",
                               "new_atoms": sorted(now - prior)})
        return 0
    return inject_state_seeds(ir_obj, Path(target), state, corpus_dir,
                              budget=STATE_BUDGET)


def _state_audit(workdir: Path, record: dict) -> None:
    path = workdir / "epochs" / "state_injections.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def run_epochs(target: str, ir: str, workdir: Path, seconds_per_epoch: float,
               epochs: int, seed: int, group: str, mode: str,
               use_feedback: bool, t0: float | None = None,
               policy: str = "reinjection") -> dict:
    """跑一组(单 epoch 连续或多 epoch 重启),返回含墙钟与累计的完整记录。

    policy:reinjection=语料联合结转(D2-R);portfolio=奇数轮正常、偶数轮
    focused(冠军组合+少量新结构,D2-P 外部预算分配)。
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

    prev_blocks = prev_edges = 0
    epoch_records = []
    for epoch in range(1, epochs + 1):
        in_dir = corpus_dir if epoch == 1 else workdir / f"corp_in_e{epoch}"
        if epoch > 1:
            in_dir.mkdir(parents=True, exist_ok=True)

        # LLM 种子注入(StallTracker 判定后才触发;失败不阻塞)
        if policy == "llm" and epoch > 1:
            try:
                _inject_llm_between_epochs(
                    ir, workdir, corpus_dir, target,
                    stall_tracker=stall_tracker, epoch_num=epoch,
                    prev_blocks=prev_blocks, prev_edges=prev_edges)
            except Exception:              # noqa: BLE001
                pass                       # 注入失败 → 照常跑下一轮
            if epoch % 2 == 0 and policy == "portfolio":
                in_dir.mkdir(parents=True, exist_ok=True)
                in_dir = _focused_corpus(workdir, workdir / "epochs")
            elif use_feedback:
                CorpusManager(workdir / "epochs" / "spool").export_seed_dir(
                    in_dir, extra=[corpus_dir])
            else:
                for f in corpus_dir.iterdir():
                    shutil.copy2(f, in_dir / f.name)

        # state 序列种子注入(P3-T3,R2 触发;与 llm 注入平级——
        # 默认路径 off/feedback/frontier/llm 不经过此分支,字节不变)
        if policy == "state" and epoch > 1:
            try:
                _inject_state_seeds_between_epochs(
                    ir, workdir, corpus_dir, target, epoch_num=epoch)
            except Exception as exc:        # noqa: BLE001
                _state_audit(workdir, {"epoch": epoch, "event": "SEAM_ERROR",
                                       "error": str(exc)})  # 失败留审计痕,照常跑
            if use_feedback:                  # 对齐 llm 臂结转:spool 冠军 ∪ lf_corpus
                CorpusManager(workdir / "epochs" / "spool").export_seed_dir(
                    in_dir, extra=[corpus_dir])
            else:                             # 无反馈:仅 lf_corpus(含注入种子)结转
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
        epoch_records.append(rec)
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
        # 3.2 诊断:组合规模/替换数;focused vs normal 的 Portfolio Yield
        state_p = workdir / "epochs" / "global_state.json"
        if state_p.exists():
            pf = __import__("json").loads(
                state_p.read_text(encoding="utf-8")).get("portfolio", {})
            result["portfolio_champions"] = len(pf.get("champions", {}))
            result["portfolio_replacements"] = pf.get("replacement_count", 0)
        if policy == "portfolio" and len(epoch_records) >= 2:
            focused = [r for i, r in enumerate(epoch_records) if (i + 1) % 2 == 0]
            normal = [r for i, r in enumerate(epoch_records) if (i + 1) % 2 == 1]
            def _yield(recs, idxs):
                # 相邻差分:每 epoch 内新增块 = 本轮末 − 上轮末(全局累计口径)
                allb = [r["blocks"] for r in epoch_records]
                gained = sum(allb[i] - allb[i - 1] if i > 0 else allb[0]
                             for i in idxs)
                secs = seconds_per_epoch * len(idxs)
                return round(gained / max(secs, 1e-9), 3)
            result["portfolio_yield"] = {
                "focused": _yield(focused, [i for i in range(len(epoch_records))
                                            if (i + 1) % 2 == 0]),
                "normal": _yield(normal, [i for i in range(len(epoch_records))
                                          if (i + 1) % 2 == 1])}
        last_in = epoch_records[-1].get("corpus_in", {})
        lf_files = _dir_stats(corpus_dir)["files"]
        result["corpus_dilution_ratio"] = round(
            max(last_in.get("files", 0) - lf_files, 0) /
            max(last_in.get("files", 0), 1), 3)
    (workdir / "group_summary.json").write_text(
        json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    return result
