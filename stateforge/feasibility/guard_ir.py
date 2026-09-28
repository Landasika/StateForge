"""Phase-4.1:BranchGuard IR——控制 frontier edge 的分支条件。

只表达"代码在哪个位置、以什么极性、由什么条件控制着这条边";
不含任何 State 概念(那是 4.2)。
"""

from __future__ import annotations

from dataclasses import dataclass

SUPPORTED_OPS = {"Eq", "NotEq", "Lt", "LtE", "Gt", "GtE"}
OP_SYMBOL = {"Eq": "==", "NotEq": "!=", "Lt": "<", "LtE": "<=", "Gt": ">",
             "GtE": ">=", "truthy": "truthy"}


@dataclass(frozen=True)
class BranchGuard:
    guard_id: str                 # file:lineno:col:op 稳定键
    source_block: str             # branch 块 id
    target_block: str             # frontier 目标块 id
    polarity: str                 # 需要的极性:"true" | "false"
    expression: str               # test 表达式源码文本
    op: str                       # Eq/NotEq/.../truthy
    lineno: int
    col_offset: int
    status: str = "ok"            # ok | UNSUPPORTED_GUARD | BRANCH_NOT_FOUND
