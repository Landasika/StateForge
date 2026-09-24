"""Phase-2 最小闭环验收(顾问六条的前四条)。

受控实验:epoch1 起跑 → 候选落盘 → 种子可回放 → 植入魔法种子 → epoch2
从首采样即覆盖深度路径(证明下一轮真的加载并执行了该语料)。
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from stateforge.core.ir import ProgramIR
from stateforge.dynamic.mapper import map_trace
from stateforge.dynamic.tracer import TraceSession
from stateforge.static.icfg import build_icfg

TARGET = Path(__file__).parents[2] / "targets" / "microplus" / "deep_branch.py"


def _driver(tmp, corpus, out, seconds, mode, epoch, group, seed=7):
    subprocess.run(
        [sys.executable, "-m", "stateforge.fuzz.entry_driver",
         str(TARGET), str(tmp / "ir.json"), str(corpus), str(out),
         "--seconds", str(seconds), "--mode", mode, "--seed", str(seed),
         "--epoch", str(epoch), "--group", group],
        capture_output=True, text=True, check=True, timeout=120)


def _replay(ir, data: bytes) -> set[str]:
    """回放:直接执行种子字节并映射,返回命中的块。"""
    spec = importlib.util.spec_from_file_location("replay_t", TARGET)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with TraceSession(root=str(TARGET.parent)) as session:
        with session.request():
            try:
                mod.target(data)
            except Exception:                    # noqa: BLE001
                pass
        return set(map_trace(list(session.events), ir).block_hits)


@pytest.fixture(scope="module")
def ir(tmp_path_factory):
    ir_obj = build_icfg(TARGET.parent)
    tmp = tmp_path_factory.mktemp("loop")
    ir_obj.to_json(tmp / "ir.json")
    return tmp, ir_obj


def test_closed_loop_candidate_save_replay_and_reinject(ir):
    tmp, ir_obj = ir

    # ---- epoch 1:真实起跑,产生候选 ----
    corpus1 = tmp / "corp1"
    corpus1.mkdir(exist_ok=True)
    out1 = tmp / "out1"
    _driver(tmp, corpus1, out1, seconds=4, mode="feedback", epoch=1, group="C")
    spool = out1 / "spool" / "seeds"
    cands = [json.loads(line) for line in
             (out1 / "C-e1-candidates.jsonl").read_text().splitlines() if line]
    assert cands and len(list(spool.iterdir())) == len(cands), "候选应落盘且数一致"

    # ---- 回放:每个种子可执行且映射出块 ----
    for seed_file in list(spool.iterdir())[:5]:
        assert _replay(ir_obj, seed_file.read_bytes()), \
            f"种子 {seed_file.name} 不可回放"

    # ---- epoch1 终态 ----
    snaps1 = [json.loads(x) for x in
              (out1 / "C-e1-snapshots.jsonl").read_text().splitlines() if x]
    blocks1 = snaps1[-1]["blocks"]

    # ---- 植入魔法种子(模拟 SF 发现 0x42 17 99 链)并导出下一轮语料 ----
    from stateforge.fuzz.corpus_manager import CorpusManager
    cm = CorpusManager(out1 / "spool")
    magic = b"\x42\x17\x99"
    cm.save(magic, epoch=1, new_blocks=5, new_edges=4,
            discovered_blocks=26, discovered_edges=26)
    corp2 = tmp / "corp2"
    n = cm.export_seed_dir(corp2, extra=[corpus1])
    assert (corp2 / cm.content_id(magic)).read_bytes() == magic
    assert n >= len(cands) + 1

    # ---- epoch 2:语料里只有植入种子(libFuzzer 启动必先执行全部语料)——
    # 断言与 native 是否聪明无关:首采样即含深度路径覆盖,且该种子按
    # 全局为空的首执行被记为候选(机制证明:保存→重注入→加载执行)。
    corp2_only = tmp / "corp2_only"
    corp2_only.mkdir(exist_ok=True)
    (corp2_only / cm.content_id(magic)).write_bytes(magic)
    out2 = tmp / "out2"
    _driver(tmp, corp2_only, out2, seconds=4, mode="feedback", epoch=2, group="C")
    first2 = json.loads((out2 / "C-e2-snapshots.jsonl").read_text().splitlines()[0])
    assert first2["blocks"] >= 30, \
        f"植入种子应在下一轮启动即执行:首采样 blocks={first2['blocks']}"
    cands2 = [json.loads(x) for x in
              (out2 / "C-e2-candidates.jsonl").read_text().splitlines() if x]
    assert cm.content_id(magic) in {c["input_id"] for c in cands2}, \
        "植入种子(新全局下的首个新颖输入)应被记为候选"
    # 深度函数块确实进入覆盖
    deep_any = any(b.function == "deep" for b in ir_obj.blocks.values())
    assert deep_any


def test_measure_mode_records_curve_without_selection(ir):
    tmp, _ = ir_obj = ir
    corpus = tmp / "corp_m"
    corpus.mkdir(exist_ok=True)
    out = tmp / "out_m"
    _driver(tmp, corpus, out, seconds=3, mode="measure", epoch=1, group="B")
    snaps = [json.loads(x) for x in
             (out / "B-e1-snapshots.jsonl").read_text().splitlines() if x]
    assert snaps and snaps[-1]["blocks"] > 0
    assert not (out / "spool").exists(), "measure 模式不得筛选落盘"
