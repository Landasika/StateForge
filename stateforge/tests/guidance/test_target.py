"""目标块与距离表的构造期校验。

重点:距离表的 None/0 必须分开,源指纹过期必须默认 fail-closed——
这两条错了,后面所有"离目标更近"的结论都不成立。
"""

from __future__ import annotations

import pytest

from stateforge.fuzzing.guidance.target import (
    BasicBlockTarget,
    InvalidTargetError,
    StaleIRError,
    TargetManager,
)
from stateforge.runtime.mapper import MappedTrace
from stateforge.tests.guidance.helpers import (
    build_target_ir,
    target_block_id,
    target_call_terminal,
)


def test_rejects_unknown_block() -> None:
    ir = build_target_ir()
    with pytest.raises(InvalidTargetError):
        TargetManager(ir, BasicBlockTarget("no-such-block"))


def test_distance_ladder_is_monotonic() -> None:
    """三层门越深越近:entry > data[0] 门 > data[1] 门 > data[2] 门 > 目标。

    不断言具体数字(它们由 ICFG 的 callsite/returnsite 结构决定,结构变则
    数字变),只断言**序**——序才是"引导方向对不对"的实质。
    """
    ir = build_target_ir()
    manager = TargetManager(ir, BasicBlockTarget(target_block_id(ir)))

    def branch_distance(expression: str) -> int:
        for block_id, block in ir.blocks.items():
            if block.kind == "branch" and expression in block.statements:
                distance = manager.distance(block_id)
                assert distance is not None
                return distance
        raise AssertionError(f"branch not found: {expression}")

    assert manager.distance(manager.block_id) == 0
    ladder = [
        manager.distance(ir.functions["target.target"].entry),
        branch_distance("<test> data[0] > 20"),
        branch_distance("<test> data[1] > 100"),
        branch_distance("<test> data[2] > 150"),
        0,
    ]
    assert all(isinstance(value, int) for value in ladder)
    assert ladder == sorted(ladder, reverse=True), f"distance ladder not monotonic: {ladder}"
    assert len(set(ladder)) == len(ladder), f"ladder has ties: {ladder}"


def test_trace_distance_uses_nearest_hit() -> None:
    ir = build_target_ir()
    manager = TargetManager(ir, BasicBlockTarget(target_block_id(ir)))
    entry = ir.functions["target.target"].entry
    near = target_call_terminal(ir)
    far_distance = manager.distance(entry)
    near_distance = manager.distance(near)
    assert far_distance is not None and near_distance is not None
    trace = MappedTrace(blocks=[entry, near])
    # 轨迹取最近命中,而不是最后命中或平均
    assert manager.distance_for_trace(trace) == near_distance < far_distance
    assert manager.nearest_block(trace) == near


def test_target_block_hit_is_zero_not_none() -> None:
    ir = build_target_ir()
    manager = TargetManager(ir, BasicBlockTarget(target_block_id(ir)))
    trace = MappedTrace(blocks=[manager.block_id])
    assert manager.distance_for_trace(trace) == 0
    assert manager.reached(trace) is True


def test_unreachable_trace_reports_none() -> None:
    """轨迹里没有任何"能到目标"的块时必须是 None,不能伪造大距离。"""
    ir = build_target_ir()
    manager = TargetManager(ir, BasicBlockTarget(target_block_id(ir)))
    trace = MappedTrace(blocks=["not-a-real-block"])
    assert manager.distance_for_trace(trace) is None
    assert manager.nearest_block(trace) is None


def test_stale_source_fails_closed(tmp_path) -> None:
    import shutil

    from stateforge.analysis.static.icfg import build_icfg

    root = tmp_path / "app"
    root.mkdir()
    shutil.copy(
        build_target_ir().blocks[target_block_id(build_target_ir())].file,
        root / "target.py")
    ir = build_icfg(root)
    manager_target = BasicBlockTarget(ir.functions["target.target_block"].entry)
    (root / "target.py").write_text("def unrelated():\n    return 0\n",
                                   encoding="utf-8")
    with pytest.raises(StaleIRError):
        TargetManager(ir, manager_target)
    # 显式放行时仍可用,但 stale 标记必须如实置位
    relaxed = TargetManager(ir, manager_target, allow_stale=True)
    assert relaxed.stale is True
