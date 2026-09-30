"""Phase-4.1B Level 5 IR:MutationHint——把 guard 求解结果写回输入的指令。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MutationHint:
    frontier_id: str             # "u->v"
    guard_id: str                # file:line:col:op
    source_kind: str             # QUERY / FORM / JSON
    source_key: str
    current_value: object         # 运行时 operand 当前值
    target_value: object          # 求解出的目标值(运行时类型)
    operator: str                 # Eq/NotEq/Lt/LtE/Gt/GtE
    desired_polarity: str         # true / false
    reason: str                   # solved / NOT_SOLVABLE_* / NOT_SINGLE_INPUT_SOLVABLE
    confidence: float = 1.0
    transform_chain: tuple = ()   # 源→变量 的冻结变换链
