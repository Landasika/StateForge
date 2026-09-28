"""Phase-4.1 Level 1:Guard 恢复精度 = 100%(声明的支持面)。"""
from pathlib import Path

import pytest

from stateforge.feasibility.guard_recovery import recover_frontier_guards
from stateforge.static.icfg import build_icfg

MICRO = Path(__file__).parents[2] / "fixtures" / "sf_branch_micro"

# 用例 → (branch 行, 期望 op, 期望表达式, 期望状态, frontier 极性)
CASES = {
    "int_eq":   (2, "Eq", "x == 731", "ok", "true"),
    "int_ne":   (2, "NotEq", "x != 0", "ok", "true"),
    "int_lt":   (2, "Lt", "x < 10", "ok", "true"),
    "int_le":   (2, "LtE", "x <= 10", "ok", "true"),
    "int_gt":   (2, "Gt", "x > 10", "ok", "true"),
    "int_ge":   (2, "GtE", "x >= 10", "ok", "true"),
    "true_frontier":  (2, "truthy", "flag", "ok", "true"),
    "false_frontier": (2, "truthy", "not flag", "ok", "true"),
    "nested_if": (3, "Eq", "y == 731", "ok", "true"),
    "unsupported_guard": (2, "UNSUPPORTED", "0 < a < 100 and b",
                          "UNSUPPORTED_GUARD", "true"),
}


def _frontier_of(ir, want_line):
    """取 test 在 want_line 的 branch 块的 true 边作 frontier。"""
    for b in ir.blocks.values():
        if b.kind == "branch" and b.start_line == want_line \
                and b.function == "run":
            for e in ir.edges:
                if e.src == b.id and e.kind == "true":
                    return (e.src, e.dst)
    raise AssertionError(f"branch@{want_line} 未找到")


@pytest.mark.parametrize("case", sorted(CASES))
def test_guard_recovery_accuracy(case):
    line, op, expr, status, polarity = CASES[case]
    ir = build_icfg(MICRO / case)
    u, v = _frontier_of(ir, line)
    guard = recover_frontier_guards(ir, [(u, v)])[0]
    assert guard.status == status, f"{case}: {guard.status}"
    assert guard.op == op
    assert guard.expression == expr
    assert guard.polarity == polarity
    assert guard.lineno == line
    assert str(MICRO / case) in guard.guard_id or guard.guard_id


def test_recovery_total_accuracy_100_percent():
    hits = misses = 0
    for case, (line, op, expr, status, polarity) in CASES.items():
        ir = build_icfg(MICRO / case)
        u, v = _frontier_of(ir, line)
        guard = recover_frontier_guards(ir, [(u, v)])[0]
        ok = (guard.status == status and guard.op == op
              and guard.expression == expr)
        hits, misses = hits + ok, misses + (not ok)
    assert misses == 0, f"GuardRecoveryAccuracy = {hits}/{hits + misses}"
