"""Phase-4.1 Level 3:Branch Distance(公式实验前冻结,禁止结果驱动修改)。

数值比较固定公式(顾问 4.1 §Level3 原文):
  目标 TRUE:  Eq |x−y| ; Ne 0/1 ; Lt max(0,x−y+1) ; LtE max(0,x−y)
              Gt max(0,y−x+1) ; GtE max(0,y−x)
  目标 FALSE = 相反谓词取距离。
  truthy/not_truthy:0/1。
字符串 Eq/Ne:相等 0 否则 1(Levenshtein 明确不做)。
不可比类型 → OPERAND_UNAVAILABLE(None),不伪造。
feedback 冻结口径:d_norm = d/(d+1)(raw 同时记录,但两口径不赛后挑选)。
"""

from __future__ import annotations

from dataclasses import dataclass

from stateforge.solving.branch_observer import BranchObservation

_NUMERIC = (int, float)
_FLIP = {"Eq": "NotEq", "NotEq": "Eq", "Lt": "GtE", "GtE": "Lt",
         "LtE": "Gt", "Gt": "LtE"}


def _numeric_distance(op: str, lhs, rhs) -> int | float:
    if op == "Eq":
        # 字符串 Eq 按冻结规格:相等=0,不等=1(非 abs 差——那是数值的)
        if isinstance(lhs, str) or isinstance(rhs, str):
            return 0 if lhs == rhs else 1
        return abs(lhs - rhs)
    if op == "NotEq":
        return 0 if lhs != rhs else 1
    if op == "Lt":
        return max(0, lhs - rhs + 1)
    if op == "LtE":
        return max(0, lhs - rhs)
    if op == "Gt":
        return max(0, rhs - lhs + 1)
    if op == "GtE":
        return max(0, rhs - lhs)
    raise ValueError(op)


@dataclass(frozen=True)
class BranchDistance:
    guard_id: str
    raw_distance: float | None
    normalized_distance: float | None
    satisfied: bool
    status: str = "ok"            # ok | OPERAND_UNAVAILABLE


def branch_distance(obs: BranchObservation, desired_polarity: str
                    ) -> BranchDistance:
    """desired_polarity:"true"/"false" —— 该 guard 控制 true/false frontier。"""
    want = desired_polarity == "true"
    op = obs.op
    if op == "truthy":
        raw = 0 if bool(obs.lhs) == want else 1
    elif op == "not_truthy":
        raw = 0 if (not bool(obs.lhs)) == want else 1
    elif isinstance(obs.lhs, _NUMERIC) and isinstance(obs.rhs, _NUMERIC):
        effective_op = op if want else _FLIP[op]
        raw = _numeric_distance(effective_op, obs.lhs, obs.rhs)
    elif op in ("Eq", "NotEq") and isinstance(obs.lhs, str) \
            and isinstance(obs.rhs, str):
        effective_op = op if want else _FLIP[op]
        raw = _numeric_distance(effective_op, obs.lhs, obs.rhs)
    else:
        return BranchDistance(obs.guard_id, None, None,
                              satisfied=obs.actual == want,
                              status="OPERAND_UNAVAILABLE")
    return BranchDistance(obs.guard_id, raw, raw / (raw + 1),
                          satisfied=(raw == 0))
