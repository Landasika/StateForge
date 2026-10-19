"""relational.py —— 关系约束状态目标 IR(契约 §6.6 编码面,验收关口 A)。

设计裁决(评审轮二/CONTRACT_DECISIONS D3-D5):
- 目标表示 = Term 关系谓词(Field/Const),**不是**业务语义字符串;
  StructuredPredicate(单主体)= Field vs Const 的退化特例,保留不动;
  SSTG 原子仍是规划词表(backward 零改动)——双轨分工:规划走 SSTG,
  判达/验证走本 IR。
- 每个关系携带源码锚点(source_file:line)、必要分支方向(via_edge)、
  原始算子(origin_op)——"关系从哪行代码得到、走哪条分支进入目标"
  可审计,不由 LLM 或名称猜测产生。
- 实体变量(?order/?user)是未绑定引用(E2),来源链见 VarProvenance;
  求值(阶段 1 结构求值器)只接受**单一快照**——结构上不可能跨时间
  拼接历史事实(完整 BindingEnv/候选枚举属阶段 3)。

诚实边界:EXISTS 的三值语义在结构求值器中收窄——实体不在快照 →
UNKNOWN(枚举完整性未知),不按契约 §2.2 判 FALSE;完整 MISSING 语义
由阶段 3 evaluate_goal 承接。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class RelOp(str, Enum):
    EQ = "EQ"
    NE = "NE"
    IN = "IN"
    NOT_IN = "NOT_IN"
    TRUTHY = "TRUTHY"
    FALSY = "FALSY"
    EXISTS = "EXISTS"          # 实体存在(left.name == "" 时为实体级)
    NOT_EXISTS = "NOT_EXISTS"


# 词表校验:op 允许的 right 形态
_NEEDS_RIGHT = frozenset({RelOp.EQ, RelOp.NE, RelOp.IN, RelOp.NOT_IN})


@dataclass(frozen=True)
class Field:
    """属性引用:实体变量 + 字段(变量身份来自 provenance,非裸字符串)。

    name == "" 表示实体级引用(仅 EXISTS/NOT_EXISTS/TRUTHY/FALSY)。
    """

    var: str                   # "?order" / "?user"(E2 未绑定实体变量)
    name: str = ""             # "user_id" / "id" / "status";""=实体本身
    source_file: str = ""
    source_line: int = 0

    def to_dict(self) -> dict:
        return {"var": self.var, "name": self.name,
                "source_file": self.source_file,
                "source_line": self.source_line}

    @classmethod
    def from_dict(cls, d: Mapping) -> "Field":
        return cls(var=str(d["var"]), name=str(d.get("name", "")),
                   source_file=str(d.get("source_file", "")),
                   source_line=int(d.get("source_line", 0)))


@dataclass(frozen=True)
class Const:
    value: Any = None

    def to_dict(self) -> dict:
        return {"value": self.value}

    @classmethod
    def from_dict(cls, d: Mapping) -> "Const":
        return cls(value=d.get("value"))


def term_from_dict(d: Mapping) -> "Field | Const":
    if "var" in d:
        return Field.from_dict(d)
    return Const.from_dict(d)


@dataclass(frozen=True)
class RelPredicate:
    """一条关系谓词:op(left, right)。right=None 仅限一元形态。

    origin_op = 源码原始算子名(如 "NotEq");via_edge = 目标经由的
    分支方向("false" = 守卫真侧早退、继续执行走假边 → 谓词已反转)。
    """

    op: RelOp
    left: Field
    right: "Field | Const | None" = None
    origin_op: str = ""          # 源码算子("Eq"/"NotEq"/"Is"/...)——反转可审计
    source_file: str = ""
    source_line: int = 0
    via_edge: str = ""           # "true"|"false"(词法早退惯用法恒 "false")
    note: str = ""               # 值盲/过近似等诚实标注

    def to_dict(self) -> dict:
        return {
            "op": self.op.value,
            "left": self.left.to_dict(),
            "right": (self.right.to_dict()
                      if isinstance(self.right, (Field, Const)) else None),
            "origin_op": self.origin_op,
            "source_file": self.source_file,
            "source_line": self.source_line,
            "via_edge": self.via_edge,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: Mapping) -> "RelPredicate":
        right = d.get("right")
        return cls(
            op=RelOp(d["op"]), left=Field.from_dict(d["left"]),
            right=term_from_dict(right) if right is not None else None,
            origin_op=str(d.get("origin_op", "")),
            source_file=str(d.get("source_file", "")),
            source_line=int(d.get("source_line", 0)),
            via_edge=str(d.get("via_edge", "")),
            note=str(d.get("note", "")),
        )

    def render(self) -> str:
        """稳定渲染(日志/测试断言用;语义比较用结构字段,非字符串)。"""
        lhs = (f"{self.left.var}.{self.left.name}" if self.left.name
               else self.left.var)
        if self.right is None:
            body = f"{self.op.value}({lhs})"
        elif isinstance(self.right, Const):
            body = f"{self.op.value}({lhs}, {self.right.value!r})"
        else:
            rhs = (f"{self.right.var}.{self.right.name}" if self.right.name
                   else self.right.var)
            body = f"{self.op.value}({lhs}, {rhs})"
        anchor = (f"@{self.source_file.rsplit('/', 1)[-1]}:"
                  f"{self.source_line}" if self.source_line else "")
        return body + anchor


@dataclass(frozen=True)
class PathCondition:
    """目标适用的路径条件(非状态谓词):配置旗标/参数/局部分支。

    例:vulnshop `if guards:` 包裹的守卫链——其内提取的关系仅在
    guards 为真时是目标的前置(否则目标不经守卫直达,关系不适用)。
    """

    var: str                    # 变量名(guards / err / oid)
    requires: bool              # True=须为真;False=须为假
    kind: str                   # config|param|route-param|local
    source_file: str = ""
    source_line: int = 0
    note: str = ""

    def to_dict(self) -> dict:
        return {"var": self.var, "requires": self.requires,
                "kind": self.kind, "source_file": self.source_file,
                "source_line": self.source_line, "note": self.note}

    @classmethod
    def from_dict(cls, d: Mapping) -> "PathCondition":
        return cls(var=str(d["var"]), requires=bool(d["requires"]),
                   kind=str(d.get("kind", "config")),
                   source_file=str(d.get("source_file", "")),
                   source_line=int(d.get("source_line", 0)),
                   note=str(d.get("note", "")))

    def render(self) -> str:
        return (f"path:{self.kind}:{self.var}"
                f"={'true' if self.requires else 'false'}")


@dataclass(frozen=True)
class VarProvenance:
    """实体变量的数据来源链(变量从哪里来;契约 §6.6.2)。

    table=None = 来源未解析(callee 不可分析/动态 SQL/外部导入)——
    变量仍可用于关系提取与绑定,但目标标注 PARTIAL(不猜表)。
    """

    var: str                        # "?order"
    table: str | None = None        # 终端容器表 "orders"
    key_param: str | None = None    # 行键参数名 "oid"(route/局部)
    origin: tuple[str, ...] = ()    # 来源链(call:…/sql:…/auth:…)
    auth_subject: bool = False      # 请求认证主体链(require_user 形态)
    source_file: str = ""
    source_line: int = 0

    def to_dict(self) -> dict:
        return {"var": self.var, "table": self.table,
                "key_param": self.key_param, "origin": list(self.origin),
                "auth_subject": self.auth_subject,
                "source_file": self.source_file,
                "source_line": self.source_line}

    @classmethod
    def from_dict(cls, d: Mapping) -> "VarProvenance":
        return cls(var=str(d["var"]), table=d.get("table"),
                   key_param=d.get("key_param"),
                   origin=tuple(d.get("origin", ())),
                   auth_subject=bool(d.get("auth_subject", False)),
                   source_file=str(d.get("source_file", "")),
                   source_line=int(d.get("source_line", 0)))

    def render(self) -> str:
        head = f"{self.var} <- " + (" | ".join(self.origin) or "?")
        if self.table:
            head += f" [table={self.table}"
            if self.key_param:
                head += f" key={self.key_param}"
            head += "]"
        if self.auth_subject:
            head += " [auth-subject]"
        return head


@dataclass(frozen=True)
class UnresolvableItem:
    """无法解析项(原因保真,不猜测)。"""

    what: str            # "var:?order" / "term:order['x'][0]" / "guard:<raw>"
    cause: str
    source_file: str = ""
    source_line: int = 0

    def to_dict(self) -> dict:
        return {"what": self.what, "cause": self.cause,
                "source_file": self.source_file,
                "source_line": self.source_line}

    @classmethod
    def from_dict(cls, d: Mapping) -> "UnresolvableItem":
        return cls(what=str(d["what"]), cause=str(d["cause"]),
                   source_file=str(d.get("source_file", "")),
                   source_line=int(d.get("source_line", 0)))


# ------------------------------------------------------------- 结构求值器

class _Unknown:
    """UNKNOWN 哨兵:≠ False/None;repr 恒 "UNKNOWN"。"""

    __slots__ = ()

    def __repr__(self) -> str:      # pragma: no cover - 调试显示
        return "UNKNOWN"

    def __bool__(self) -> bool:     # pragma: no cover - 防隐式真值
        raise TypeError("UNKNOWN 无真值;显式与 UNKNOWN 比较,禁止隐式转换")


UNKNOWN: Any = _Unknown()


def resolve(term: "Field | Const", snapshot: Mapping[str, Mapping],
            bindings: Mapping[str, str]) -> Any:
    """term → 值 | UNKNOWN(阶段 1 结构求值;单一快照输入)。

    - Const → 值;
    - Field:实体变量未绑定 → UNKNOWN;绑定实体不在快照 → UNKNOWN
      (阶段 1 边界:不区分"枚举确认缺失"(MISSING→EXISTS=FALSE)与
      "未观测"——完整语义属阶段 3 evaluate_goal);
      实体在快照但字段缺 → UNKNOWN。
    """
    if isinstance(term, Const):
        return term.value
    entity = bindings.get(term.var)
    if entity is None:
        return UNKNOWN
    row = snapshot.get(entity)
    if not isinstance(row, Mapping):
        return UNKNOWN
    if term.name == "":              # 实体级引用:行在场即"有值"
        return row
    return row.get(term.name, UNKNOWN)


def evaluate_relation(pred: RelPredicate, snapshot: Mapping[str, Mapping],
                      bindings: Mapping[str, str]) -> str:
    """单条关系 → "TRUE" | "FALSE" | "UNKNOWN"。"""
    op = pred.op
    if op in (RelOp.EXISTS, RelOp.NOT_EXISTS):
        entity = bindings.get(pred.left.var)
        if entity is None:
            return "UNKNOWN"
        present = entity in snapshot
        if op is RelOp.EXISTS:
            return "TRUE" if present else "UNKNOWN"
        # NOT_EXISTS:快照无枚举完整性信息,证伪不了"不存在"→ 结构求值
        # 只能给 FALSE(在场)或 UNKNOWN;诚实边界,不猜。
        return "FALSE" if present else "UNKNOWN"

    left = resolve(pred.left, snapshot, bindings)
    if op in (RelOp.TRUTHY, RelOp.FALSY):
        if left is UNKNOWN:
            return "UNKNOWN"
        truth = bool(left)
        return "TRUE" if (truth if op is RelOp.TRUTHY else not truth) \
            else "FALSE"

    if pred.right is None or op not in _NEEDS_RIGHT:
        return "UNKNOWN"                      # 形态不完整,不猜
    right = resolve(pred.right, snapshot, bindings)
    if left is UNKNOWN or right is UNKNOWN:
        return "UNKNOWN"
    try:
        if op is RelOp.EQ:
            return "TRUE" if left == right else "FALSE"
        if op is RelOp.NE:
            return "TRUE" if left != right else "FALSE"
        if op is RelOp.IN:
            return "TRUE" if left in right else "FALSE"
        return "TRUE" if left not in right else "FALSE"
    except (TypeError, ValueError):           # 类型不可比(如 int in str)
        return "UNKNOWN"


def evaluate_relations(relations, snapshot: Mapping[str, Mapping],
                       bindings: Mapping[str, str]) -> str:
    """关系合取(阶段 1):任一 FALSE → FALSE;全 TRUE → TRUE;否则 UNKNOWN。

    同一绑定赋值贯穿全部关系(?order/?user 不许各自挑最有利实例);
    快照为单次输入,结构上排除 t1/t2 拼接(阶段 3 验收反例)。
    """
    result = "TRUE"
    for pred in relations:
        v = evaluate_relation(pred, snapshot, bindings)
        if v == "FALSE":
            return "FALSE"
        if v == "UNKNOWN":
            result = "UNKNOWN"
    return result
