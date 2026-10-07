"""Phase-4.3 语义状态模型(StateVariable / StatePredicate / AbstractState)。

与 Phase-5A 的 StateFactCandidate(静态提取用)不同,
这些模型服务于状态抽象和动态状态发现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StateSource(str, Enum):
    SESSION = "session"


class StateOp(str, Enum):
    READ = "read"
    SET = "set"
    UPDATE = "update"
    DELETE = "delete"
    CLEAR = "clear"


@dataclass(frozen=True)
class StateVariable:
    source: StateSource
    key: str

    @property
    def canonical(self) -> str:
        return f"{self.source.value}.{self.key}"


class PredicateOp(str, Enum):
    EQ = "EQ"
    NE = "NE"
    GT = "GT"
    GE = "GE"
    LT = "LT"
    LE = "LE"
    IS_NONE = "IS_NONE"
    IS_NOT_NONE = "IS_NOT_NONE"
    IN = "IN"
    NOT_IN = "NOT_IN"
    TRUTHY = "TRUTHY"
    FALSY = "FALSY"


@dataclass(frozen=True)
class StatePredicate:
    variable: StateVariable
    op: PredicateOp
    value: Any = None

    def to_dict(self) -> dict:
        return {"var": self.variable.canonical,
                "op": self.op.value, "value": self.value}


@dataclass(frozen=True)
class StateAccess:
    variable: StateVariable
    operation: StateOp
    endpoint: str | None
    function: str | None
    filename: str
    lineno: int
    value_expr: str | None = None
    guard_expr: str | None = None


# ── 抽象域 ──

@dataclass(frozen=True)
class DomainRule:
    """变量 → 抽象值映射规则。"""
    variable: StateVariable
    name: str               # 抽象值名(如 "EQ(admin)" / "[3,5)" / "NOT_NONE")
    predicate: StatePredicate

    def matches(self, concrete: Any) -> bool:
        return _eval(self.predicate, concrete)


@dataclass(frozen=True)
class AbstractValue:
    variable: StateVariable
    name: str

    def to_dict(self):
        return {"var": self.variable.canonical, "val": self.name}


@dataclass(frozen=True)
class AbstractState:
    values: tuple[AbstractValue, ...]   # 每个变量的抽象值

    @property
    def canonical_key(self) -> str:
        return "|".join(
            f"{v.variable.canonical}={v.name}" for v in self.values)

    def to_dict(self) -> dict:
        return {v.variable.canonical: v.name for v in self.values}


def _eval(pred: StatePredicate, val: Any) -> bool:
    try:
        if pred.op == PredicateOp.EQ:
            return val == pred.value
        if pred.op == PredicateOp.NE:
            return val != pred.value
        if pred.op == PredicateOp.GT:
            return val is not None and val > pred.value
        if pred.op == PredicateOp.GE:
            return val is not None and val >= pred.value
        if pred.op == PredicateOp.LT:
            return val is not None and val < pred.value
        if pred.op == PredicateOp.LE:
            return val is not None and val <= pred.value
        if pred.op == PredicateOp.IS_NONE:
            return val is None
        if pred.op == PredicateOp.IS_NOT_NONE:
            return val is not None
        if pred.op == PredicateOp.IN:
            return val in pred.value
        if pred.op == PredicateOp.NOT_IN:
            return val not in pred.value
        if pred.op == PredicateOp.TRUTHY:
            return bool(val)
        if pred.op == PredicateOp.FALSY:
            return not bool(val)
    except (TypeError, ValueError):
        return False
    return False
