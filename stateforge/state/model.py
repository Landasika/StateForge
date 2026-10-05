"""Phase-5A:StateFact 模型——持久状态事实的 IR。

StateFact = 可能影响未来控制流可达性的持久事实。
与 semantic 层的 StateCandidate(LLM lifting 用)解耦。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StateDomain(str, Enum):
    SESSION = "SESSION"
    DATABASE = "DATABASE"
    FILE = "FILE"


class StateOperation(str, Enum):
    READ = "READ"
    WRITE = "WRITE"
    DELETE = "DELETE"


class Relevance(str, Enum):
    CONTROL = "CONTROL"           # 直接出现在分支守卫中
    NON_CONTROL = "NON_CONTROL"   # 只在非守卫上下文使用
    UNKNOWN = "UNKNOWN"           # 间接数据流/复杂模式


class PredicateOp(str, Enum):
    EQ = "EQ"
    NE = "NE"
    EXISTS = "EXISTS"
    NOT_EXISTS = "NOT_EXISTS"
    IN = "IN"
    NOT_IN = "NOT_IN"
    TRUTHY = "TRUTHY"
    FALSY = "FALSY"


@dataclass(frozen=True)
class StructuredPredicate:
    operator: PredicateOp
    subject: str                   # canonical object_ref
    value: Any = None              # 比较值(None = EXISTS/TRUTHY)

    def to_dict(self) -> dict:
        return {"operator": self.operator.value,
                "subject": self.subject,
                "value": self.value}

    @classmethod
    def from_dict(cls, d: dict) -> "StructuredPredicate":
        return cls(PredicateOp(d["operator"]), d["subject"], d.get("value"))


@dataclass(frozen=True)
class StateFactCandidate:
    candidate_id: str              # deterministic: {domain}:{object_ref}:{op}:{file}:{line}
    domain: StateDomain
    object_ref: str                # canonical: session.user_id / Order.status / FILE:/path
    operation: StateOperation
    source_file: str
    function_name: str | None
    lineno: int
    block_id: str | None           # ProgramIR block(可选)
    predicate: str | None          # 原始表达式文本
    structured_predicate: StructuredPredicate | None
    written_value: Any             # 写入值(None = UNKNOWN)
    relevance: Relevance
    evidence: tuple[str, ...]      # (file:line description)

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "domain": self.domain.value,
            "object_ref": self.object_ref,
            "operation": self.operation.value,
            "source_file": self.source_file,
            "function_name": self.function_name,
            "lineno": self.lineno,
            "block_id": self.block_id,
            "predicate": self.predicate,
            "structured_predicate": (
                self.structured_predicate.to_dict()
                if self.structured_predicate else None),
            "written_value": self.written_value,
            "relevance": self.relevance.value,
            "evidence": list(self.evidence),
        }
