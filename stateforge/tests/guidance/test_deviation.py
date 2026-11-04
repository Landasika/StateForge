"""偏差分析:只报"确实走错分支"的点,且证据齐备(双方后继+距离)。

不许把"普通顺序相邻"或"两个后继一样近"当成偏差——那会给出无法执行的
翻谓词建议,把静态可解率做高而实际无用。
"""

from __future__ import annotations

import textwrap

import pytest

from stateforge.analysis.static.icfg import build_icfg
from stateforge.fuzzing.guidance.deviation import (
    DeviationAnalyzer,
    DeviationPoint,
    find_deviation_points,
)
from stateforge.runtime.mapper import MappedTrace


def build(tmp_path, source: str):
    (tmp_path / "app.py").write_text(textwrap.dedent(source), encoding="utf-8")
    return build_icfg(tmp_path)


def branch_after(ir, expression: str) -> str:
    for block_id, block in ir.blocks.items():
        if block.kind == "branch" and any(
                expression in s for s in block.statements):
            return block_id
    raise AssertionError(f"branch not found: {expression}")


def test_detects_wrong_branch_direction(tmp_path) -> None:
    """先走 a<100 的假支(离目标远),应报为偏差点。"""
    ir = build(tmp_path, """
        def f(a):
            if a < 100:
                return "far"
            if a > 200:
                return "TARGET"
            return "mid"
    """)
    branch = branch_after(ir, "a < 100")
    true_successor = next(e.dst for e in ir.outgoing(branch) if e.kind == "true")
    false_successor = next(e.dst for e in ir.outgoing(branch) if e.kind == "false")
    target = ir.functions["app.f"].entry
    # 目标设在 false 之后的那条链上
    for block_id, block in ir.blocks.items():
        if any("TARGET" in s for s in block.statements):
            target = block_id
    trace = MappedTrace(blocks=[branch, true_successor],
                        edges=[(branch, true_successor)],
                        edge_kinds={(branch, true_successor): {"true"}})
    points = find_deviation_points(ir, target, trace)
    assert points, "should detect deviation"
    point = points[0]
    assert isinstance(point, DeviationPoint)
    assert point.block_id == branch
    assert point.observed_successor == true_successor
    assert point.observed_successor != false_successor
    assert 0.0 < point.confidence <= 1.0
    assert point.desired_distance < (point.observed_distance or 10**9)


def test_no_deviation_when_already_on_best_path(tmp_path) -> None:
    ir = build(tmp_path, """
        def f(a):
            if a < 100:
                return "far"
            if a > 200:
                return "TARGET"
            return "mid"
    """)
    branch = branch_after(ir, "a < 100")
    false_successor = next(e.dst for e in ir.outgoing(branch) if e.kind == "false")
    target = next(bid for bid, b in ir.blocks.items()
                  if any("TARGET" in s for s in b.statements))
    trace = MappedTrace(blocks=[branch, false_successor],
                        edges=[(branch, false_successor)],
                        edge_kinds={(branch, false_successor): {"false"}})
    assert find_deviation_points(ir, target, trace) == []


def test_plain_adjacency_is_not_deviation(tmp_path) -> None:
    """没有 true/false 边的相邻转移不算偏差(它根本不是分支选择)。"""
    ir = build(tmp_path, """
        def f(a):
            entry = a
            if a > 1:
                return "TARGET"
            return "other"
    """)
    target = next(bid for bid, b in ir.blocks.items()
                  if any("TARGET" in s for s in b.statements))
    entry = ir.functions["app.f"].entry
    trace = MappedTrace(blocks=[entry], edges=[])
    assert find_deviation_points(ir, target, trace) == []


def test_unknown_successor_distance_still_reported_with_lower_confidence(
        tmp_path) -> None:
    ir = build(tmp_path, """
        def f(a):
            if a < 100:
                return "far"
            if a > 200:
                return "TARGET"
            return "mid"
    """)
    branch = branch_after(ir, "a < 100")
    true_successor = next(e.dst for e in ir.outgoing(branch) if e.kind == "true")
    target = next(bid for bid, b in ir.blocks.items()
                  if any("TARGET" in s for s in b.statements))
    analyzer = DeviationAnalyzer(ir, target)
    distances = dict(analyzer.distances)
    distances.pop(true_successor, None)
    trace = MappedTrace(blocks=[branch, true_successor],
                        edges=[(branch, true_successor)],
                        edge_kinds={(branch, true_successor): {"true"}})
    points = find_deviation_points(ir, target, trace, distances)
    assert points and points[0].observed_distance is None
    # 观测侧距离未知 → 置信度按上限 0.8 给,不高于确定情形之上限
    assert points[0].confidence <= 0.8


def test_mapping_failures_reduce_confidence(tmp_path) -> None:
    ir = build(tmp_path, """
        def f(a):
            if a < 100:
                return "far"
            if a > 200:
                return "TARGET"
            return "mid"
    """)
    branch = branch_after(ir, "a < 100")
    true_successor = next(e.dst for e in ir.outgoing(branch) if e.kind == "true")
    target = next(bid for bid, b in ir.blocks.items()
                  if any("TARGET" in s for s in b.statements))
    clean = MappedTrace(blocks=[branch, true_successor],
                        edges=[(branch, true_successor)],
                        edge_kinds={(branch, true_successor): {"true"}})
    noisy = MappedTrace(blocks=[branch, true_successor],
                        edges=[(branch, true_successor)],
                        edge_kinds={(branch, true_successor): {"true"}},
                        unresolved=[object()])
    clean_points = find_deviation_points(ir, target, clean)
    noisy_points = find_deviation_points(ir, target, noisy)
    assert clean_points and noisy_points
    assert noisy_points[0].confidence < clean_points[0].confidence


def test_unknown_target_rejected(tmp_path) -> None:
    ir = build(tmp_path, "def f(a):\n    return a\n")
    with pytest.raises(ValueError):
        DeviationAnalyzer(ir, "no-such-block")


def test_results_are_deterministically_ordered(tmp_path) -> None:
    ir = build(tmp_path, """
        def f(a, b):
            if a < 100:
                return "far"
            if b < 50:
                return "also_far"
            if a > 200:
                return "TARGET"
            return "mid"
    """)
    target = next(bid for bid, b in ir.blocks.items()
                  if any("TARGET" in s for s in b.statements))
    pairs = []
    for expression in ("a < 100", "b < 50"):
        branch = branch_after(ir, expression)
        true_successor = next(e.dst for e in ir.outgoing(branch) if e.kind == "true")
        pairs.append((branch, true_successor))
    trace = MappedTrace(blocks=[b for b, _ in pairs],
                        edges=pairs,
                        edge_kinds={pair: {"true"} for pair in pairs})
    first = find_deviation_points(ir, target, trace)
    second = find_deviation_points(ir, target, trace)
    assert [p.block_id for p in first] == [p.block_id for p in second]
