"""集成:引导必须真的把搜索拉向目标(定向 vs 均匀,同预算)。

统计断言用同一组随机种子成对比较,而不是单次运行——单次运行受采样噪声
支配,结论不可靠。断言的是"定向臂不劣于均匀臂且在深层靶上更优",
而不是"定向臂每次都最快"(那是过强且不成立的断言)。
"""

from __future__ import annotations

import pytest

from stateforge.fuzzing.guidance.campaign import (
    CampaignConfig,
    GuidedCampaign,
)
from stateforge.fuzzing.guidance.target import BasicBlockTarget
from stateforge.tests.guidance.helpers import (
    adapter,
    build_target_ir,
    seeds,
    target_block_id,
)


def run_arm(scheduler: str, seed: int, budget: int = 3000) -> int | None:
    ir = build_target_ir()
    campaign = GuidedCampaign(
        ir, BasicBlockTarget(target_block_id(ir)), adapter(),
        config=CampaignConfig(max_executions=budget, random_seed=seed,
                              scheduler_name=scheduler))
    result = campaign.run(seeds())
    return result.executions_to_target


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
def test_both_arms_reach_target(seed: int) -> None:
    assert run_arm("guided", seed) is not None
    assert run_arm("uniform", seed) is not None


def test_guided_arm_is_not_worse_on_average() -> None:
    seeds_to_try = list(range(12))
    guided = [run_arm("guided", seed) for seed in seeds_to_try]
    uniform = [run_arm("uniform", seed) for seed in seeds_to_try]
    assert all(value is not None for value in guided), guided
    assert all(value is not None for value in uniform), uniform
    guided_mean = sum(guided) / len(guided)
    uniform_mean = sum(uniform) / len(uniform)
    assert guided_mean <= uniform_mean, (
        f"guided arm slower: guided={guided_mean:.1f} uniform={uniform_mean:.1f}")


def test_distance_actually_decreases_over_a_run() -> None:
    """运行中最好的距离必须单调下降——这是"距离反馈生效"的直接证据。"""
    ir = build_target_ir()
    campaign = GuidedCampaign(
        ir, BasicBlockTarget(target_block_id(ir)), adapter(),
        config=CampaignConfig(max_executions=400, random_seed=3,
                              stop_on_target=False))
    result = campaign.run(seeds())
    assert result.best_distance is not None
    assert result.best_distance <= 18
    assert result.unique_covered_blocks > 0


def test_campaign_reaches_via_public_cli_entry(tmp_path) -> None:
    """CLI 入口可用(不经 pytest 也能跑通的最小回归)。"""
    from stateforge.cli import main
    import sys

    ir = build_target_ir()
    argv = [
        "guided", str(ir.metadata["root"]),
        "--block", target_block_id(ir),
        "--adapter", "stateforge.tests.guidance.helpers:adapter",
        "--seeds", str(build_target_ir().metadata["root"] + "/seeds"),
        "--executions", "2000",
        "--out", str(tmp_path / "cli-out"),
    ]
    original = sys.argv
    sys.argv = ["stateforge"] + argv
    try:
        assert main() == 0
    finally:
        sys.argv = original
    assert (tmp_path / "cli-out" / "summary.json").is_file()


def test_cli_reports_unknown_block(tmp_path) -> None:
    from stateforge.cli import main
    import sys

    ir = build_target_ir()
    original = sys.argv
    sys.argv = ["stateforge", "guided", str(ir.metadata["root"]),
                "--block", "no-such-block",
                "--adapter", "stateforge.tests.guidance.helpers:adapter",
                "--seeds", str(ir.metadata["root"] + "/seeds"),
                "--out", str(tmp_path / "bad")]
    try:
        assert main() == 2
    finally:
        sys.argv = original
