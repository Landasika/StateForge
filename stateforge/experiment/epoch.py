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

from stateforge.fuzz.corpus_manager import CorpusManager

EXECUTION_EDGE_KINDS = {"flow", "true", "false", "call", "return"}


def _dir_stats(d: Path) -> dict:
    files = [f for f in d.iterdir() if f.is_file()] if d.exists() else []
    return {"files": len(files), "bytes": sum(f.stat().st_size for f in files)}


def _run_driver(target: str, ir: str, corpus_dir: Path, out_dir: Path,
                seconds: float, mode: str, seed: int, group: str,
                epoch: int) -> None:
    cmd = [sys.executable, "-m", "stateforge.fuzz.entry_driver",
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


def run_epochs(target: str, ir: str, workdir: Path, seconds_per_epoch: float,
               epochs: int, seed: int, group: str, mode: str,
               use_feedback: bool, t0: float | None = None) -> dict:
    """跑一组(单 epoch 连续或多 epoch 重启),返回含墙钟与累计的完整记录。"""
    t0 = t0 if t0 is not None else time.monotonic()
    workdir.mkdir(parents=True, exist_ok=True)
    corpus_dir = workdir / "lf_corpus"
    corpus_dir.mkdir(exist_ok=True)
    epoch_records = []
    for epoch in range(1, epochs + 1):
        in_dir = corpus_dir if epoch == 1 else workdir / f"corp_in_e{epoch}"
        if epoch > 1:
            in_dir.mkdir(exist_ok=True)
            if use_feedback:
                CorpusManager(workdir / "epochs" / "spool").export_seed_dir(
                    in_dir, extra=[corpus_dir])
            else:
                for f in corpus_dir.iterdir():
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
    if use_feedback:
        result["novel_seed_gain"] = _novel_seed_gain(workdir)
    (workdir / "group_summary.json").write_text(
        json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    return result
