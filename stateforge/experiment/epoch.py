"""Phase-2 模块 4:EpochController——固定时间预算起 Atheris 子进程,语料交接。

- B 组(重启对照):下一轮种子目录 = libFuzzer 自身语料结转(原地目录拷贝)
- C 组(反馈):下一轮 = SF 新颖种子 ∪ libFuzzer 语料结转
- 汇总从子进程落盘的 snapshots/candidates 重建(子进程以 _Exit 退出)。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path


def _run_driver(target: str, ir: str, corpus_dir: Path, out_dir: Path,
                seconds: float, mode: str, seed: int, group: str,
                epoch: int) -> None:
    cmd = [sys.executable, "-m", "stateforge.fuzz.entry_driver",
           target, ir, str(corpus_dir), str(out_dir),
           "--seconds", str(seconds), "--mode", mode,
           "--seed", str(seed), "--epoch", str(epoch), "--group", group]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds * 4 + 60)
    if proc.returncode not in (0, -2, 245):    # libFuzzer _Exit 可能带非零码
        raise RuntimeError(f"driver 失败 rc={proc.returncode}: "
                           f"{proc.stderr[-2000:]}")


def _summaries(out_dir: Path, group: str, epoch: int) -> dict:
    snap_path = out_dir / f"{group}-e{epoch}-snapshots.jsonl"
    cand_path = out_dir / f"{group}-e{epoch}-candidates.jsonl"
    snaps = [json.loads(line) for line in
             snap_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    cands = [json.loads(line) for line in
             cand_path.read_text(encoding="utf-8").splitlines() if line.strip()] \
        if cand_path.exists() else []
    last = snaps[-1] if snaps else {"executions": 0, "blocks": 0, "edges": 0, "t": 0}
    return {"group": group, "epoch": epoch,
            "executions": last.get("executions", 0),
            "wall_seconds": last.get("t", 0),
            "blocks": last.get("blocks", 0), "edges": last.get("edges", 0),
            "candidates_saved": len(cands),
            "snapshots": snaps}


def run_group_epochs(target: str, ir: str, workdir: Path, seconds_per_epoch: float,
                     epochs: int, seed: int, group: str, use_feedback: bool,
                     restart: bool) -> list[dict]:
    """跑一个组的全部 epoch,返回逐 epoch 汇总。"""
    workdir.mkdir(parents=True, exist_ok=True)
    # B 组(重启对照)用 measure:与 C 相同的计量开销、不做筛选——
    # 这样 B vs C 的差异只来自"结构反馈筛选";off 仅用于 A 的连续裸跑。
    mode = "feedback" if use_feedback else "measure"
    corpus_dir = workdir / "lf_corpus"
    corpus_dir.mkdir(exist_ok=True)
    results = []
    for epoch in range(1, epochs + 1):
        in_dir = corpus_dir if epoch == 1 else workdir / f"corp_in_e{epoch}"
        if epoch > 1:
            in_dir.mkdir(exist_ok=True)
            if use_feedback:
                from stateforge.fuzz.corpus_manager import CorpusManager
                CorpusManager(workdir / "epochs" / "spool").export_seed_dir(
                    in_dir, extra=[corpus_dir])
            else:
                for f in corpus_dir.iterdir():     # B:libFuzzer 语料结转
                    shutil.copy2(f, in_dir / f.name)
        out_dir = workdir / "epochs"
        _run_driver(target, ir, in_dir, out_dir, seconds_per_epoch,
                    mode if restart else mode, seed, group, epoch)
        # libFuzzer 把新单元写进输入目录;统一拷回 lf_corpus 供下一轮结转
        if in_dir != corpus_dir:
            for f in in_dir.iterdir():
                if f.is_file():
                    shutil.copy2(f, corpus_dir / f.name)
        results.append(_summaries(out_dir, group, epoch))
    return results
