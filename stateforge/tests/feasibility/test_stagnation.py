"""Phase-4.2-A/B/C 单元测试:plateau / stalled / slice / 触发组合。"""
import json
from pathlib import Path

import pytest

from stateforge.core.ir import ProgramIR, BasicBlock, FlowEdge, FunctionIR
from stateforge.solving.branch_observer import BranchObservation
from stateforge.solving.input_provenance import resolve_operand_source
from stateforge.solving.plateau import PlateauDetector
from stateforge.solving.semantic_slice import build_slice
from stateforge.solving.stalled import find_stalled
from stateforge.solving.stagnation import StagnationMonitor


# ---------------------------------------------------------------- A plateau

def test_plateau_triggers_after_frozen_window():
    det = PlateauDetector(window=100)
    status = None
    for i in range(1, 250):
        status = det.observe(i, 50, 60, 5)
    assert status == "STAGNATED"


def test_plateau_not_triggered_while_growing():
    det = PlateauDetector(window=100)
    for i in range(1, 300):
        s = det.observe(i, 50 + i // 10, 60 + i // 20, 5)
        assert s == "RUNNING"


def test_plateau_hysteresis_only_crossing_resumes():
    det = PlateauDetector(window=100)
    for i in range(1, 250):
        det.observe(i, 50, 60, 5)
    assert det.status == "STAGNATED"
    # 新块不解锁(滞回)
    for i in range(250, 400):
        det.observe(i, 99, 99, 5)
    assert det.status == "STAGNATED"
    # crossing 才解锁
    s = det.observe(400, 99, 99, 6)
    assert s == "RUNNING"


# ---------------------------------------------------------------- B stalled

def _ir():
    ir = ProgramIR(metadata={"root": "/tmp"})
    f = "/t/app.py"
    for bid, lo in [("b1", 1), ("gate", 2), ("easy", 3), ("deep", 4),
                    ("unreached_src", 5)]:
        ir.blocks[bid] = BasicBlock(bid, f, "app", "h", lo, lo, "block", [],
                                    [lo])
    ir.functions["app.h"] = FunctionIR("app.h", f, "app", "b1", "deep",
                                       ["b1", "gate", "easy", "deep"])
    for s, d in [("b1", "gate"), ("b1", "easy"), ("gate", "deep")]:
        ir.edges.append(FlowEdge(s, d, "flow"))
    return ir


def test_stalled_requires_min_hits():
    ir = _ir()
    covered_edges = {("b1", "easy")}
    hits = {"b1": 500, "gate": 50, "easy": 500}
    # gate 块命中不够(50 < 100)→ 不算 stalled
    assert find_stalled(ir, hits, covered_edges, min_hits=100) == []


def test_stalled_finds_blocked_edge():
    import ast as _ast
    import tempfile
    # 真 guard 源文件
    src = "def h(x):\n    if x == 731:\n        return 1\n    return 0\n"
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "app.py"
        p.write_text(src)
        ir = ProgramIR(metadata={"root": d})
        f = str(p)
        ir.blocks["entry"] = BasicBlock("entry", f, "app", "h", 1, 1,
                                        "entry", [], [1])
        ir.blocks["br"] = BasicBlock("br", f, "app", "h", 2, 2, "branch", [],
                                     [2])
        ir.blocks["t"] = BasicBlock("t", f, "app", "h", 3, 3, "terminal", [],
                                    [3])
        ir.functions["app.h"] = FunctionIR("app.h", f, "app", "entry", "t",
                                           ["entry", "br", "t"])
        ir.edges.append(FlowEdge("br", "t", "true"))
        ir.edges.append(FlowEdge("entry", "br", "flow"))
        hits = {"br": 500}
        stalled = find_stalled(ir, hits, set())
        assert len(stalled) == 1
        assert stalled[0].guard.expression == "x == 731"
        assert stalled[0].source_hit_count == 500


# ---------------------------------------------------------------- C slice

def test_slice_seven_components_complete(tmp_path):
    app = tmp_path / "app.py"
    app.write_text("def h(request):\n    x = int(request.args['x'])\n"
                   "    if x == 731:\n        return prize()\n"
                   "    return 0\n\ndef prize():\n    return 'hit'\n",
                   encoding="utf-8")
    from stateforge.analysis.static.icfg import build_icfg
    ir = build_icfg(tmp_path)
    branch_id = next(b.id for b in ir.blocks.values()
                     if b.kind == "branch" and b.function == "h")
    covered = {branch_id: 200}
    stalled = find_stalled(ir, covered, set())
    assert stalled, "应找到 x==731 的 stalled frontier"
    sf = stalled[0]
    obs = [BranchObservation(sf.guard.guard_id, "Eq", 20, 731, False, i)
           for i in range(1, 4)]
    prov = resolve_operand_source(app, "h", sf.guard.lineno)
    slice_ = build_slice(ir, sf, obs, prov, app,
                         corpus_meta=[{"input_id": "aa", "distance": 700,
                                       "interesting_reason": "frontier_champion",
                                       "nearest_frontier": sf.frontier_id}])
    # 七项全齐
    assert set(slice_) == {"target_frontier", "guard", "local_cfg",
                           "backward_dataflow", "input_source", "runtime",
                           "failed_mutations"}
    assert slice_["guard"]["expression"] == "x == 731"
    assert slice_["local_cfg"]["function"] == "h"
    assert any("request.args" in s for s in slice_["backward_dataflow"])
    assert slice_["input_source"]["kind"] == "QUERY"
    assert slice_["input_source"]["transform_chain"] == ["int"]
    assert len(slice_["runtime"]["distance_history"]) == 3
    assert slice_["runtime"]["distance_history"][-1]["distance_raw"] == 711
    assert slice_["failed_mutations"][0]["input_id"] == "aa"
    # JSON 可序列化(LLM 输入即此格式)
    json.dumps(slice_, ensure_ascii=False)


# ------------------------------------------------------------ 触发组合器

def test_trigger_requires_all_three(tmp_path):
    app = tmp_path / "app.py"
    app.write_text("def h(request):\n    x = int(request.args['x'])\n"
                   "    if x == 731:\n        return prize()\n"
                   "    return 0\n\ndef prize():\n    return 'hit'\n",
                   encoding="utf-8")
    from stateforge.analysis.static.icfg import build_icfg
    ir = build_icfg(tmp_path)
    mon = StagnationMonitor(ir, app, window=50, min_hits=10)
    mon.deterministic_failure = False
    branch_id = next(b.id for b in ir.blocks.values()
                     if b.kind == "branch" and b.function == "h")
    hits = {}
    # 喂 200 次执行:branch 块被击中但 edge 未跨 → plateau + stalled
    for i in range(1, 201):
        hits = {branch_id: i}
        t = mon.observe(i, hits, {("unreal", "edge")}, 0)
    assert t.plateau == "STAGNATED" and t.stalled_count >= 1
    assert not t.triggered, "确定性求解未失败 → 不触发"
    mon.deterministic_failure = True
    t = mon.observe(201, hits, set(), 0)
    assert t.triggered and t.slices, "三条件齐 → 触发并携带切片"
    assert set(t.slices[0]) == {"target_frontier", "guard", "local_cfg",
                                "backward_dataflow", "input_source",
                                "runtime", "failed_mutations"}
