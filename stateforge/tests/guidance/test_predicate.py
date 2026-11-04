"""谓词提取:可解/不可解的边界必须显式,不能"猜一个谓词"。

所有不可解情形都返回 ``UnsupportedPredicate`` 并带原因,不抛异常——
上游据此统计静态可解率,而不是被单点异常打断。
"""

from __future__ import annotations

import textwrap

import pytest

from stateforge.analysis.static.icfg import build_icfg
from stateforge.analysis.static.predicate import (
    PredicateExtractor,
    PredicateInfo,
    UnsupportedPredicate,
    extract_predicate,
    extract_predicates,
)


def build(tmp_path, source: str):
    (tmp_path / "app.py").write_text(textwrap.dedent(source), encoding="utf-8")
    return build_icfg(tmp_path)


def branch_block(ir, expression: str) -> str:
    for block_id, block in ir.blocks.items():
        if block.kind == "branch" and any(
                expression in statement for statement in block.statements):
            return block_id
    raise AssertionError(f"branch not found: {expression}")


def test_extracts_all_supported_operators(tmp_path) -> None:
    ir = build(tmp_path, """
        def f(a, b):
            if a == 1:
                return "eq"
            if a != 2:
                return "ne"
            if a > 3:
                return "gt"
            if a >= 4:
                return "ge"
            if a < 5:
                return "lt"
            if a <= 6:
                return "le"
            return "none"
    """)
    extraction = extract_predicates(ir)
    assert {item.operator for item in extraction.predicates} == {
        "==", "!=", ">", ">=", "<", "<="}
    assert extraction.unsupported == []


def test_predicate_records_successors_and_literal(tmp_path) -> None:
    ir = build(tmp_path, """
        def f(a):
            if a > 20:
                return "hit"
            return "miss"
    """)
    extraction = extract_predicates(ir)
    assert len(extraction.predicates) == 1
    predicate = extraction.predicates[0]
    assert predicate.expression == "a > 20"
    assert predicate.literal == "20"
    assert predicate.literal_side == "right"
    assert predicate.true_successor is not None
    assert predicate.false_successor is not None
    assert predicate.true_successor != predicate.false_successor
    # 后继与布尔结果的对应关系必须自洽
    assert predicate.outcome_for(predicate.true_successor) is True
    assert predicate.outcome_for(predicate.false_successor) is False
    assert predicate.outcome_for("unrelated") is None


def test_literal_on_left_side(tmp_path) -> None:
    ir = build(tmp_path, """
        def f(a):
            if 42 == a:
                return "hit"
            return "miss"
    """)
    predicate = extract_predicates(ir).predicates[0]
    assert predicate.literal == "42"
    assert predicate.literal_side == "left"


def test_predicate_ids_are_unique_and_stable(tmp_path) -> None:
    ir = build(tmp_path, """
        def f(a):
            if a > 1:
                return "x"
            return "y"
    """)
    first = extract_predicates(ir).predicates[0].predicate_id
    second = extract_predicates(ir).predicates[0].predicate_id
    assert first == second
    assert first.startswith("pred_")


def test_by_block_and_get_helpers(tmp_path) -> None:
    ir = build(tmp_path, """
        def f(a):
            if a > 1:
                return "x"
            return "y"
    """)
    extraction = extract_predicates(ir)
    block_id = branch_block(ir, "a > 1")
    assert extraction.get(block_id) is not None
    assert block_id in extraction.by_block()
    assert set(extraction.by_id()) == {p.predicate_id for p in extraction.predicates}


@pytest.mark.parametrize("expression,reason", [
    ("a and b", "boolean_expression"),
    ("a or b", "boolean_expression"),
    ("f(a)", "function_call"),
    ("not a", "unary_expression"),
])
def test_unsupported_expressions_are_classified(tmp_path, expression, reason) -> None:
    ir = build(tmp_path, f"""
        def f(a, b):
            if {expression}:
                return "x"
            return "y"
    """)
    extraction = extract_predicates(ir)
    assert extraction.predicates == []
    assert len(extraction.unsupported) == 1
    assert extraction.unsupported[0].reason == reason


def test_chained_comparison_is_unsupported(tmp_path) -> None:
    ir = build(tmp_path, """
        def f(a, b, c):
            if a < b < c:
                return "x"
            return "y"
    """)
    extraction = extract_predicates(ir)
    assert extraction.predicates == []
    assert extraction.unsupported[0].reason == "chained_comparison"


def test_unsupported_operand_rejected(tmp_path) -> None:
    """含调用/推导式的操作数静态不可判定,必须拒绝而不是估一个值。"""
    ir = build(tmp_path, """
        def g():
            return 1

        def f(a):
            if a > g():
                return "x"
            return "y"
    """)
    extraction = extract_predicates(ir)
    assert extraction.predicates == []
    assert extraction.unsupported[0].reason == "unsupported_operand"


def test_attribute_and_subscript_operands_supported(tmp_path) -> None:
    ir = build(tmp_path, """
        class C:
            limit = 10

        def f(a, items):
            if a > C.limit:
                return "x"
            if items[0] > 5:
                return "y"
            return "z"
    """)
    extraction = extract_predicates(ir)
    assert {p.expression for p in extraction.predicates} == {
        "a > C.limit", "items[0] > 5"}


def test_missing_block_reports_reason() -> None:
    ir = build_target_ir()
    item = extract_predicate(ir, "does-not-exist")
    assert isinstance(item, UnsupportedPredicate)
    assert item.reason == "missing_block"


def test_extract_specific_blocks_only(tmp_path) -> None:
    ir = build(tmp_path, """
        def f(a, b):
            if a > 1:
                return "x"
            if b > 2:
                return "y"
            return "z"
    """)
    block_id = branch_block(ir, "a > 1")
    extraction = extract_predicates(ir, [block_id])
    assert len(extraction.predicates) == 1
    assert extraction.predicates[0].block_id == block_id


def test_extract_block_returns_info_instance(tmp_path) -> None:
    ir = build(tmp_path, """
        def f(a):
            if a > 1:
                return "x"
            return "y"
    """)
    block_id = branch_block(ir, "a > 1")
    assert isinstance(PredicateExtractor(ir).extract_block(block_id), PredicateInfo)


def test_unsupported_for_lookup(tmp_path) -> None:
    ir = build(tmp_path, """
        def f(a, b):
            if a and b:
                return "x"
            return "y"
    """)
    extraction = extract_predicates(ir)
    block_id = branch_block(ir, "a and b")
    assert extraction.unsupported_for(block_id) is not None


def test_source_unavailable_is_reported(tmp_path) -> None:
    """源码不可读时不伪造谓词,而是给出 source_unavailable。"""
    ir = build(tmp_path, """
        def f(a):
            if a > 1:
                return "x"
            return "y"
    """)
    (tmp_path / "app.py").unlink()
    extraction = extract_predicates(ir)
    assert extraction.predicates == []
    assert extraction.unsupported[0].reason in {
        "source_unavailable", "source_syntax_error"}


from stateforge.tests.guidance.helpers import build_target_ir  # noqa: E402
