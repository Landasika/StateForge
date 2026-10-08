"""Phase-4.1B Level 5:BranchSolver——按冻结规则表生成 MutationHint。

规则(实验前冻结,禁止结果驱动修改):
  desired TRUE : Eq→C   NotEq→C+1(数值)/C+"x"(字符串)
                 Lt→C-1  LtE→C   Gt→C+1  GtE→C
  desired FALSE: 先转补谓词(!=↔==, <↔>=, <=↔>, >↔<=, >=↔<),再走 TRUE 表。
字符串仅 Eq/Ne。truthy 无常量 → 不解。类型不匹配 → 拒绝。
"""

from __future__ import annotations

from stateforge.solving.branch_observer import BranchObservation
from stateforge.fuzzing.mutation_hint import MutationHint

_FLIP = {"Eq": "NotEq", "NotEq": "Eq", "Lt": "GtE", "GtE": "Lt",
         "LtE": "Gt", "Gt": "LtE"}
# 输入在 RHS 时把 C op x 规范化为 x op' C(P0-1:先 canonicalize 再翻极性)
_SWAP = {"Eq": "Eq", "NotEq": "NotEq", "Lt": "Gt", "Gt": "Lt",
         "LtE": "GtE", "GtE": "LtE"}


def _solve_true(op: str, const):
    if op == "Eq":
        return const
    if op == "NotEq":
        return const + 1 if isinstance(const, (int, float)) else f"{const}x"
    if op == "Lt":
        return const - 1
    if op == "LtE":
        return const
    if op == "Gt":
        return const + 1
    if op == "GtE":
        return const
    raise ValueError(op)


def solve(frontier_id: str, guard_id: str, polarity: str, op: str,
          input_is_lhs: bool, observation: BranchObservation,
          source_kind: str, source_key: str) -> MutationHint:
    current = observation.lhs if input_is_lhs else observation.rhs
    const = observation.rhs if input_is_lhs else observation.lhs
    want = (polarity == "true")
    canonical_op = op if input_is_lhs else _SWAP[op]   # INPUT op CONST
    effective_op = canonical_op if want else _FLIP[canonical_op]
    reason = "solved"
    target = None
    if not isinstance(const, (int, float, str)):
        reason = "NOT_SOLVABLE_NON_CONSTANT"
    elif isinstance(current, bool) or op not in _FLIP:
        reason = "NOT_SOLVABLE_UNSUPPORTED_OP"
    elif isinstance(const, str) != isinstance(current, str) \
            and isinstance(const, (int, float)) != isinstance(current, (int, float)):
        reason = "NOT_SOLVABLE_TYPE_MISMATCH"
    elif isinstance(const, str) and effective_op not in ("Eq", "NotEq"):
        reason = "NOT_SOLVABLE_STRING_ORDERED"
    else:
        target = _solve_true(effective_op, const)
    return MutationHint(
        frontier_id=frontier_id, guard_id=guard_id,
        source_kind=source_kind, source_key=source_key,
        current_value=current, target_value=target,
        operator=op, desired_polarity=polarity, reason=reason,
        confidence=1.0 if reason == "solved" else 0.0)
