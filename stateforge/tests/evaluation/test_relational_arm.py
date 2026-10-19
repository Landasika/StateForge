"""test_relational_arm.py —— PR-07:无 LLM 四臂消融的协议与指标纪律。

验证:分桶不混计(proxy/verified/witness)、A–C 无观测面不伪造覆盖、
D 的 witness-only 覆盖口径(诚实 <100%)、公平字段(同预算/同种子
确定性)、JSONL/summary 可复核。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stateforge.evaluation.experiment.relational_arm import run_ablation

VULNSHOP = Path(__file__).parents[2] / "targets" / "vulnshop"
BUDGET = 90


@pytest.fixture(scope="module")
def summary(tmp_path_factory):
    out = tmp_path_factory.mktemp("relarm")
    return run_ablation(VULNSHOP, out, budget_requests=BUDGET, seed=7)


def _arm(summary, name):
    return next(a for a in summary["arms"] if a["arm"] == name)


def test_fairness_fields(summary):
    assert summary["seed"] == 7 and summary["budget_requests"] == BUDGET
    for a in summary["arms"]:
        assert a["requests_executed"] == BUDGET      # 同预算(请求粒度)
        assert a["wall_seconds"] > 0                  # 成本单独统计
        assert a["inputs"] >= 1


def test_metric_buckets_never_mixed(summary):
    for a in summary["arms"]:
        # 四桶键各自独立存在,不合并为单一成功率
        assert set(a["goal_coverage"]) >= {"achieved", "total"}
        assert "instance_state_novelty" in a
        assert "verified_transition_novelty" in a
        assert a["proxy_progress"]["atoms"] or a["arm"] == "A"
        # 目标块覆盖(Trace)与状态判达分别输出
        assert isinstance(a["target_block_covered"], dict)


def test_unobserved_arms_do_not_fake_coverage(summary):
    for name in ("A", "B", "C"):
        a = _arm(summary, name)
        assert a["goal_coverage"]["achieved"] is None
        assert "无观测面" in a["goal_coverage"]["note"]
        assert a["instance_state_novelty"] is None
        assert a["verified_transition_novelty"] is None


def test_d_witness_only_coverage_honest(summary):
    d = _arm(summary, "D")
    achieved = d["goal_coverage"]["achieved"]
    total = d["goal_coverage"]["total"]
    # set_role 需 admin 路径,90 请求预算内不可达 → 诚实 <100%
    assert 0 < len(achieved) < total
    assert "app.py::set_role" not in " ".join(achieved)
    # witness-only:achieved 均有 witness 结构
    assert all(w["witness"] for w in d["goal_witnesses"])
    assert d["instance_state_novelty"] > 0
    assert d["verified_transition_novelty"] > 0


def test_determinism_same_seed(tmp_path):
    s1 = run_ablation(VULNSHOP, tmp_path / "d1", budget_requests=40,
                      seed=11)
    s2 = run_ablation(VULNSHOP, tmp_path / "d2", budget_requests=40,
                      seed=11)
    for a1, a2 in zip(s1["arms"], s2["arms"]):
        assert a1["requests_executed"] == a2["requests_executed"]
        assert a1["target_block_covered"] == a2["target_block_covered"]


def test_jsonl_and_summary_artifacts(tmp_path):
    run_ablation(VULNSHOP, tmp_path, budget_requests=30, seed=3,
                 arms=("A", "D"))
    jsonl = tmp_path / "relational_arm.jsonl"
    lines = [json.loads(x) for x in
             jsonl.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(lines) == 2 and {r["arm"] for r in lines} == {"A", "D"}
    for r in lines:                                  # C11 必备键
        for key in ("arm", "requests_executed", "inputs", "wall_seconds",
                    "target_block_covered", "goal_coverage",
                    "instance_state_novelty",
                    "verified_transition_novelty", "proxy_progress",
                    "notes"):
            assert key in r
    s = json.loads((tmp_path / "relational_arm_summary.json")
                   .read_text(encoding="utf-8"))
    assert s["seed"] == 3 and len(s["arms"]) == 2 \
        and s["goal_denominator"]
