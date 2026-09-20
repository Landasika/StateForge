"""core/model.py —— SF1-T1 全模型(驱动侧 pydantic v2;监督侧只用 stdlib)。

R1/ADR-0009 候选锚定标注的数据面:
  StateCandidate(静态挖掘,未裁决) → 标注(规则|LLM) → StateVar(label+rationale)
  → 确定性选择器 → SSTG(graph/builder.py)

每条事实带代码锚点(file:line)——SA-PR 的"程序表示"性质。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

HttpMethod = Literal["GET", "POST", "PUT", "DELETE", "PATCH"]

# 六类安全标签 + irrelevant(SF1 六类漏洞锚定表)
StateLabel = Literal[
    "authn", "authz", "ownership", "money", "workflow", "single_use", "irrelevant"
]
CandidateSource = Literal["session_access", "db_field", "condition", "ownership_binding"]


class Param(BaseModel):
    name: str
    location: Literal["path", "query", "json", "header", "cookie"]
    py_type: str = "str"
    value_domain: list[str] | None = None


class StateCandidate(BaseModel):
    """静态挖掘的未裁决事实(ADR-0009):patterns 产出,不做安全裁决。"""

    var: str                      # 规范名,如 "Order.status" / "Session.authed_as"
    entity: str | None = None
    attr: str | None = None
    ops: list[Literal["read", "compare", "write"]] = Field(default_factory=list)
    evidence: str                 # 人读证据(代码片段摘录)
    source: CandidateSource
    file: str
    line: int


class StateVar(BaseModel):
    """候选标注后的晋升态(标签落此处,LLM/规则写 label+rationale)。"""

    entity: str
    attr: str
    kind: Literal["db_column", "session", "derived"] = "db_column"
    domain: list[str] | None = None
    label: StateLabel | None = None
    security_relevant: bool = False
    rationale: str = ""
    anchors: list[tuple[str, int]] = Field(default_factory=list)  # [(file, line), ...]

    @property
    def var(self) -> str:
        return f"{self.entity}.{self.attr}"


class Effect(BaseModel):
    var: str                      # "Order.status" / "Session.authed_as"
    op: Literal["set", "clear", "check"]
    value: str | None = None
    key: str | None = None        # 实体键绑定,如 "id=?oid"
    file: str
    line: int


class Precondition(BaseModel):
    var: str
    op: Literal["eq", "ne", "in", "truthy", "falsy"]
    value: str | None = None
    key: str | None = None
    file: str
    line: int


class Guard(BaseModel):
    kind: Literal["role", "owner", "token", "custom"]
    present: bool = True          # False = 该端点缺失此类守卫(漏洞信号)
    detail: str = ""
    file: str
    line: int


class Sink(BaseModel):
    kind: Literal["sql_build", "file_path", "exec", "redirect"]
    file: str
    line: int
    snippet: str = ""


class Endpoint(BaseModel):
    func: str
    route: str
    methods: list[HttpMethod]
    params: list[Param] = Field(default_factory=list)
    guards: list[Guard] = Field(default_factory=list)
    preconditions: list[Precondition] = Field(default_factory=list)
    effects: list[Effect] = Field(default_factory=list)
    sinks: list[Sink] = Field(default_factory=list)
    observes: list[str] = Field(default_factory=list)
    file: str = ""
    line: int = 0


class PlanStep(BaseModel):
    """一步动作:端点 + 参数槽(?var 引用绑定,@fresh 自造)。"""

    action: str                       # "POST /auth/register"
    json: dict[str, str] = Field(default_factory=dict)
    path: dict[str, str] = Field(default_factory=dict)      # 路径参数(已代入或 ?var)
    headers: dict[str, str] = Field(default_factory=dict)   # 如 Authorization: Bearer ?token
    extract: dict[str, str] = Field(default_factory=dict)   # 响应键 → ?var


class PlanSegment(BaseModel):
    """一条规划链 = 一个 epoch(二阶乘积律的工程落地)。"""

    goal: str
    steps: list[PlanStep] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)          # 规划期备注(soft producer 等)


class Invariant(BaseModel):
    """同实体状态原子间的合法迁移(静态 guard→effect 配对推导,ADR-0009)。"""

    entity: str
    attr: str
    legal_transitions: list[tuple[str, str]] = Field(default_factory=list)
    one_way: list[str] = Field(default_factory=list)  # 单向布尔,如 Coupon.used

    @property
    def var(self) -> str:
        return f"{self.entity}.{self.attr}"


class AppModel(BaseModel):
    """SA-PR v1:分析产物的根。"""

    app_root: str
    framework: Literal["flask", "connexion", "django"]
    endpoints: list[Endpoint] = Field(default_factory=list)
    candidates: list[StateCandidate] = Field(default_factory=list)
    state_vars: list[StateVar] = Field(default_factory=list)
    invariants: list[Invariant] = Field(default_factory=list)
    config_flags: dict[str, int] = Field(default_factory=dict)

    def to_json(self, path: str | Path) -> None:
        Path(path).write_text(self.model_dump_json(indent=2), encoding="utf-8")

    @classmethod
    def from_json(cls, path: str | Path) -> "AppModel":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))
