"""Campaign 端到端:预算、命中、证据落盘、对照臂与故障隔离。

核心断言是**证据口径**:
* ``target_reached`` 只在真命中目标块时为真;
* ``nearest_block`` 只是"走到哪",不得被当成命中;
* 落盘的 ``target_reaching_input.bin`` 必须能重放出命中。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stateforge.adapters.callable import CallableAdapter
from stateforge.fuzzing.guidance.campaign import (
    CampaignConfig,
    GuidedCampaign,
)
from stateforge.fuzzing.guidance.scheduler import EmptyCorpusError
from stateforge.fuzzing.guidance.target import BasicBlockTarget
from stateforge.tests.guidance.helpers import (
    adapter,
    build_target_ir,
    replay,
    seeds,
    target_block_id,
)


def make_campaign(tmp_path: Path, **config_kwargs) -> GuidedCampaign:
    ir = build_target_ir()
    config = CampaignConfig(output_dir=tmp_path / "out", **config_kwargs)
    return GuidedCampaign(ir, BasicBlockTarget(target_block_id(ir)),
                          adapter(), config=config)


def test_campaign_reaches_target_and_writes_evidence(tmp_path) -> None:
    campaign = make_campaign(tmp_path, max_executions=4000, random_seed=3)
    result = campaign.run(seeds())
    assert result.target_reached is True
    assert result.best_distance == 0
    assert result.executions_to_target is not None
    assert result.termination_reason == "target_reached"
    out = tmp_path / "out"
    for name in ("config.json", "metrics.jsonl", "summary.json"):
        assert (out / name).is_file(), f"missing evidence file: {name}"
    # 落盘的目标输入必须能重放出命中——这是"证据"而非"日志"的区别
    target_file = out / "target_reaching_input.bin"
    assert target_file.is_file()
    from stateforge.targets.guidance_m1 import target as module
    assert module.target(target_file.read_bytes()) == 3


def test_summary_records_target_and_config(tmp_path) -> None:
    campaign = make_campaign(tmp_path, max_executions=2000, random_seed=1)
    campaign.run(seeds())
    summary = json.loads((tmp_path / "out" / "summary.json").read_text())
    assert summary["target_reached"] is True
    assert summary["target_basic_block"] == target_block_id(build_target_ir())
    assert summary["config"]["max_executions"] == 2000
    assert summary["environment"]["dependencies"] is not None
    assert "scheduler" in summary and "mutation" in summary


def test_metrics_lines_match_executions(tmp_path) -> None:
    campaign = make_campaign(tmp_path, max_executions=50, random_seed=5,
                             stop_on_target=False)
    result = campaign.run(seeds())
    lines = (tmp_path / "out" / "metrics.jsonl").read_text().splitlines()
    assert len(lines) == result.executions
    first = json.loads(lines[0])
    for key in ("execution", "candidate_distance", "target_reached",
                "distance_improved", "nearest_block", "valid_edge_count",
                "unmatched_edge_count"):
        assert key in first


def test_budget_is_respected(tmp_path) -> None:
    campaign = make_campaign(tmp_path, max_executions=10, random_seed=0,
                             stop_on_target=False)
    result = campaign.run([b"AB"])
    assert result.executions == 10
    assert result.termination_reason == "max_executions"


def test_max_seconds_is_respected(tmp_path) -> None:
    campaign = make_campaign(tmp_path, max_executions=10**9, max_seconds=0.0,
                             random_seed=0)
    result = campaign.run(seeds())
    assert result.executions == 0
    assert result.termination_reason == "max_seconds"


def test_empty_corpus_rejected(tmp_path) -> None:
    campaign = make_campaign(tmp_path, max_executions=10)
    with pytest.raises(EmptyCorpusError):
        campaign.run([])


def test_deterministic_for_same_seed(tmp_path) -> None:
    def run() -> tuple[int, int | None, int]:
        campaign = make_campaign(tmp_path / str(id(object())),
                                 max_executions=200, random_seed=11,
                                 stop_on_target=False)
        result = campaign.run(seeds())
        return (result.executions, result.executions_to_target,
                result.unique_covered_blocks)

    assert run() == run()


def test_uniform_arm_reaches_target_too(tmp_path) -> None:
    """对照臂必须也能命中——否则比较的是"会不会跑"而不是"引导好不好"。"""
    campaign = make_campaign(tmp_path, max_executions=5000, random_seed=0,
                             scheduler_name="uniform")
    result = campaign.run(seeds())
    assert result.target_reached is True
    assert result.best_distance == 0


def test_adapter_fault_is_isolated(tmp_path) -> None:
    """适配器抛异常只记一次 error,不终止 campaign(默认 continue)。"""
    def boom(data: bytes):
        raise RuntimeError("adapter exploded")

    ir = build_target_ir()
    campaign = GuidedCampaign(
        ir, BasicBlockTarget(target_block_id(ir)), CallableAdapter(boom),
        config=CampaignConfig(output_dir=tmp_path / "out",
                              max_executions=5, random_seed=0,
                              stop_on_target=False))
    result = campaign.run([b"AB"])
    assert result.executions == 5
    assert result.unexpected_exceptions == 5
    assert (tmp_path / "out" / "crashes").is_dir()


def test_fault_policy_stop_halts(tmp_path) -> None:
    def boom(data: bytes):
        raise RuntimeError("adapter exploded")

    ir = build_target_ir()
    campaign = GuidedCampaign(
        ir, BasicBlockTarget(target_block_id(ir)), CallableAdapter(boom),
        config=CampaignConfig(output_dir=tmp_path / "out",
                              max_executions=50, random_seed=0,
                              fault_policy="stop", stop_on_target=False))
    result = campaign.run([b"AB"])
    assert result.executions == 1
    assert result.termination_reason == "fault"


def test_covered_edges_are_static_valid_only(tmp_path) -> None:
    """covered_edges 只含 ICFG 上真实存在的边;相邻但无静态边的不计入。"""
    campaign = make_campaign(tmp_path, max_executions=100, random_seed=0,
                             stop_on_target=False)
    result = campaign.run(seeds())
    valid = {(edge.src, edge.dst) for edge in campaign.ir.edges}
    assert result.unique_valid_icfg_edges > 0
    assert result.unique_valid_icfg_edges <= len(valid)
    for seed in campaign.corpus.seeds:
        assert seed.covered_edges <= valid


def test_reached_target_is_not_confused_with_nearest(tmp_path) -> None:
    """走到最近块但没进目标块时,reached 必须为假。"""
    campaign = make_campaign(tmp_path, max_executions=1, random_seed=0,
                             stop_on_target=False)
    result = campaign.execute_input(b"AB")
    assert result.target_reached is False
    assert result.nearest_block is not None
    assert result.distance is not None and result.distance > 0


def test_replay_input_hits_on_first_execution(tmp_path) -> None:
    campaign = make_campaign(tmp_path, max_executions=10, random_seed=0)
    result = campaign.execute_input(replay())
    assert result.target_reached is True
    assert result.distance == 0


def test_invalid_configurations_rejected(tmp_path) -> None:
    ir = build_target_ir()
    target = BasicBlockTarget(target_block_id(ir))
    for kwargs in (
        {"max_executions": -1},
        {"max_corpus_size": 0},
        {"epsilon": 2.0},
        {"fault_policy": "bogus"},
        {"min_energy": 10, "max_energy": 1},
        {"scheduler_name": "bogus"},
        {"selection_policy": "bogus"},
        {"energy_policy": "bogus"},
        {"fixed_energy": 0},
        {"progress_every": 0},
    ):
        with pytest.raises(ValueError):
            CampaignConfig(**kwargs)
    with pytest.raises(ValueError):
        GuidedCampaign(ir, target, object())


def test_legacy_scheduler_name_maps_to_policies() -> None:
    guided = CampaignConfig(scheduler_name="guided")
    assert (guided.selection_policy, guided.energy_policy) == ("distance", "distance")
    uniform = CampaignConfig(scheduler_name="uniform")
    assert (uniform.selection_policy, uniform.energy_policy) == ("uniform", "fixed")
