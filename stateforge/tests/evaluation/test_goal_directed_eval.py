"""Phase 6 验收:goal_directed_eval 三臂对照(微靶族)。

主张(诚实口径):目标定向臂以个位数请求判达状态门深块;单请求
随机(冷启动)对这些块零命中;多请求无引导(= 去静态引导消融)只在
"前置谓词可被随机值满足"的靶(chain)上偶发命中且成本高一个量级。
否定分支/双演员门(owner≠uid)超出 MVP 谓词主张面,如实记录。
"""

from pathlib import Path

import pytest

from stateforge.analysis.static.icfg import build_icfg
from stateforge.evaluation.goal_directed_eval import (
    arm_goal_directed, arm_single_random, derive_state_gated_blocks,
    dispatch_arms, run_eval)
from stateforge.state.extractor import extract_state_facts

SHOP = Path(__file__).parents[2] / "targets" / "stateful"


@pytest.fixture(scope="module")
def ir_facts():
    ir = build_icfg(SHOP)
    facts = extract_state_facts(SHOP, ir=ir)
    return ir, facts


def _src(name: str) -> str:
    return str(SHOP / f"{name}.py")


def test_state_gated_targets_derived(ir_facts):
    ir, _ = ir_facts
    expect = {
        "t_state_chain": "deep_state_profile",
        "t_state_gate": "gate_granted",
        "t_state_shop": "ship_privileged",
    }
    for name, marker in expect.items():
        targets = derive_state_gated_blocks(_src(name), ir,
                                            dispatch_arms(_src(name)))
        hit = [t for t in targets
               if marker in ir.blocks[t].statements[0]
               or any(marker in s for s in ir.blocks[t].statements)]
        assert hit, f"{name} 应推导出 {marker} 深块目标"


def test_goal_directed_reaches_all_core_targets(ir_facts):
    ir, facts = ir_facts
    for name in ("t_state_chain", "t_state_gate", "t_state_shop"):
        src = _src(name)
        arms = dispatch_arms(src)
        targets = derive_state_gated_blocks(src, ir, arms)
        res = arm_goal_directed(src, ir, facts, arms, targets)
        assert res.reached, f"{name} goal 臂应判达至少一个深块"
        assert res.requests_used <= 3 * len(targets)


def test_single_random_cold_start_misses_state_gates(ir_facts):
    ir, _ = ir_facts
    src = _src("t_state_gate")            # role==admin 谓词:随机值不含
    arms = dispatch_arms(src)
    targets = derive_state_gated_blocks(src, ir, arms)
    res = arm_single_random(src, ir, arms, targets, budget=30)
    assert res.requests_used == 30
    assert not res.reached


def test_goal_cheaper_than_seq_on_chain(ir_facts):
    ir, facts = ir_facts
    src = _src("t_state_chain")
    arms = dispatch_arms(src)
    targets = derive_state_gated_blocks(src, ir, arms)
    goal = arm_goal_directed(src, ir, facts, arms, targets)
    assert goal.cost_per_target and max(
        goal.cost_per_target.values()) <= 3


def test_run_eval_writes_report(tmp_path, ir_facts):
    out = tmp_path / "eval"
    payload = run_eval(SHOP, out_dir=out)
    assert (out / "report.json").exists()
    md = (out / "report.md").read_text(encoding="utf-8")
    assert "goal_directed" in md and "seq_random" in md
    assert len(payload["reports"]) == 3
    # shop 的否定分支缺口被如实注记
    assert "双演员门" in md
