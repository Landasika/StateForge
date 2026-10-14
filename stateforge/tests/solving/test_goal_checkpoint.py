"""Phase 5 验收:检查点存档 + C10 种子注入 + 复用重放。

判达的执行结果 → 检查点(JSON 往返)→ sequence_seed(与消费端
逐字节互认)→ 语料注入(spool 惯例)→ 复用重放零推导再判达。
"""

from pathlib import Path

from stateforge.analysis.static.icfg import build_icfg
from stateforge.runtime.plan_executor import (_load_target_module,
                                              execute_plan)
from stateforge.solving.goal_checkpoint import (GoalCheckpoint,
                                                checkpoint_from_execution,
                                                inject_checkpoint_seed,
                                                replay_checkpoint)
from stateforge.state.extractor import extract_state_facts
from stateforge.state.goal_to_plan import dispatch_arms, plan_for_block

SHOP = Path(__file__).parents[2] / "targets" / "stateful"
TARGET = "t_state_shop.py::_handle::91-92::block::1"
SRC = str(SHOP / "t_state_shop.py")


def _run(tmp_path):
    ir = build_icfg(SHOP)
    facts = extract_state_facts(SHOP, ir=ir)
    goal, segment = plan_for_block(ir, facts, TARGET)
    mod = _load_target_module(SRC)
    res = execute_plan(segment, mod, ir, facts, TARGET, SRC,
                       dispatch_arms(SRC))
    ck = checkpoint_from_execution(TARGET, SRC, goal, res)
    return ir, facts, res, ck


def test_checkpoint_requires_verified_reach():
    """未判达的执行不产检查点(不能只凭'执行过'就存档)。"""
    import dataclasses
    from stateforge.runtime.plan_executor import PlanExecution

    ir, facts, res, _ck = _run(None)
    failed = dataclasses.replace(res, target_covered=False)
    ck = checkpoint_from_execution(TARGET, SRC, _FakeGoal(), failed)
    assert ck is None


class _FakeGoal:
    constraints = ()


def test_checkpoint_roundtrip(tmp_path):
    ir, facts, _res, ck = _run(tmp_path)
    path = ck.save(tmp_path / "ckpts")
    loaded = GoalCheckpoint.load(path)
    assert loaded.checkpoint_id == ck.checkpoint_id
    assert [s["json"]["op"] for s in loaded.steps] == [
        "register", "create_order", "ship_order"]
    assert len(loaded.goal_constraints) == 3
    assert "ship_privileged" in loaded.hits


def test_sequence_seed_c10_format():
    import json

    _ir, _f, _r, ck = _run(None)
    seed = ck.sequence_seed()
    parsed = json.loads(seed)
    assert list(parsed) == ["steps"]
    assert all("json" in s for s in parsed["steps"])


def test_inject_into_corpus(tmp_path):
    _ir, _f, _r, ck = _run(tmp_path)
    corpus = tmp_path / "work" / "corpus"
    cid = inject_checkpoint_seed(ck, corpus)
    assert cid
    assert (corpus / cid).read_bytes() == ck.sequence_seed()
    spool_seeds = tmp_path / "work" / "goal_spool" / "seeds"
    assert (spool_seeds / cid).exists()          # spool 双写
    assert inject_checkpoint_seed(ck, corpus) is None   # 去重


def test_replay_from_checkpoint_re_reaches(tmp_path):
    """复用重放:检查点 steps 直接执行(零推导),目标块仍判达。"""
    ir, facts, _res, ck = _run(tmp_path)
    replay = replay_checkpoint(ck, ir, facts, dispatch_arms(SRC))
    assert replay.target_covered
    assert "ship_privileged" in replay.hits
    assert all(s.ok for s in replay.steps)
