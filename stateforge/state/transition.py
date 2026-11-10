"""Phase-P3 M2:Transition Discovery(动态边记录 + 静态候选)。

M1 的 StateRegistry 只产抽象状态节点(按 canonical_key 全局去重),
request_id 被丢弃——没有边。本模块补迁移发现两侧:

- 动态:`TransitionRecorder` 在同一逻辑会话内逐请求观察 session
  快照,抽象状态(canonical_key)变化即向 registry 记一条
  `TransitionEdge`(触发者 = 请求标签,evidence = dynamic:state-diff)。
  **会话边界显式**:`new_session()` 重置"当前节点"——StateRegistry
  是全局节点去重的,而边只连接同一逻辑会话内相邻两次观察,跨会话
  不得连线;会话重置只影响边的 from 端,不污染全局注册表。
- 静态:两个**分开的**事实源(概念不混):
  * `static_transition_points` —— **状态转移点**(语句级):session
    SET/UPDATE 变异。转移点就是那条赋值语句本身,执行它持久状态
    即变;不需要配守卫、不锚定"两端"。
  * `static_guard_candidates` —— **守卫**(读侧,另一类事实):session
    谓词判断 × 所属函数。它回答"这段代码需要什么状态才能过",
    与"谁改变了状态"是两类事实;规划时 join 使用,但不属于转移的定义。
  转移**记录**(边)由动态层派生:请求执行到转移点前后各拍快照,
  差即 from→to(`TransitionRecorder`);静态层只给点,不给边。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from stateforge.state.observer import Abstractor, StateNode, StateRegistry
from stateforge.state.predicate_extractor import _resolve_condition
from stateforge.state.semantic_model import StateOp
from stateforge.state.session_access import analyze_session_access
from stateforge.state.state_provenance import SessionProvenance


@dataclass(frozen=True)
class TransitionEdge:
    """一条有向迁移:from 状态 --trigger--> to 状态。

    from_key/to_key 用 AbstractState.canonical_key(与 StateRegistry
    的节点去重键同一词表);evidence 为证据串列表:动态边固定
    ["dynamic:state-diff"],静态候选边为 ["<file>:<line>"]。
    """

    from_key: str
    to_key: str
    trigger: str
    evidence: list[str] = field(default_factory=list)

    @property
    def dedup_key(self) -> tuple[str, str, str]:
        """StateRegistry.add_edge 的去重键。"""
        return (self.from_key, self.to_key, self.trigger)


def _trigger_label(request_id) -> str:
    """请求标签:str 原样(端点/函数名);int 视作序列位置 → "req-<n>"。"""
    return request_id if isinstance(request_id, str) else f"req-{request_id}"


class TransitionRecorder:
    """逐请求观察 session,向 StateRegistry 记录动态迁移边。

    用法:harness 在每个请求执行后调用 ``observe(request_id,
    session_dict)``(请求后快照);首个观察只建立"当前节点"。

    会话边界(显式设计):``new_session(initial=None)`` 开启新逻辑
    会话并重置当前节点(给 initial 则先注册为起点,不产边)。
    之后的边一律从该会话内的前一个节点出发——两个逻辑会话即使
    复用 registry 中同一全局节点也不连线。
    """

    def __init__(self, registry: StateRegistry, abstractor: Abstractor):
        self.registry = registry
        self.abstractor = abstractor
        self._current: StateNode | None = None
        self._tick = 0

    def new_session(self, initial: dict | None = None) -> None:
        """显式会话边界:重置当前节点;initial 非空则注册为起点(不产边)。"""
        self._current = None
        if initial is not None:
            self._current = self._register(initial)

    def observe(self, request_id, session: dict) -> StateNode:
        """观察 request_id 之后快照;相对当前节点抽象状态变化即记边。"""
        self._tick += 1
        node = self.registry.observe(self.abstractor.abstract(session),
                                     self._tick)
        prev, self._current = self._current, node
        if prev is not None and prev.state.canonical_key != node.state.canonical_key:
            self.registry.add_edge(TransitionEdge(
                from_key=prev.state.canonical_key,
                to_key=node.state.canonical_key,
                trigger=_trigger_label(request_id),
                evidence=["dynamic:state-diff"]))
        return node

    def _register(self, session: dict) -> StateNode:
        self._tick += 1
        return self.registry.observe(self.abstractor.abstract(session),
                                     self._tick)


@dataclass(frozen=True)
class StaticCandidate:
    """静态事实(证据级):转移点或守卫,按 op 前缀区分。

    转移点:op 为 StateOp 值("set"/"update")——执行即改变持久状态。
    守卫:op 为 "guard:<OP>(<value>)"(如 guard:EQ(admin))——读侧
    判断,不改状态。两类事实分开产出(`static_transition_points` /
    `static_guard_candidates`),此处共用一个记录形状。
    evidence = ["<file>:<line>"]。
    """

    function: str
    var: str
    op: str
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"function": self.function, "var": self.var,
                "op": self.op, "evidence": list(self.evidence)}


_MUTATION_OPS = frozenset({StateOp.SET, StateOp.UPDATE})


def static_transition_points(source_dir) -> list[StaticCandidate]:
    """状态转移点(session SET/UPDATE 变异)× 所属函数。

    转移点 = 语句本身;守卫**不**在此列(见 `static_guard_candidates`)。
    """
    out: list[StaticCandidate] = []
    for access in analyze_session_access(source_dir):
        if access.operation in _MUTATION_OPS:
            out.append(StaticCandidate(
                function=access.function or "<module>",
                var=access.variable.canonical,
                op=access.operation.value,
                evidence=[f"{access.filename}:{access.lineno}"]))
    return out


def static_guard_candidates(source_dir) -> list[StaticCandidate]:
    """守卫(session 谓词判断)× 所属函数(读侧事实,非转移)。

    复用 predicate_extractor._resolve_condition(同一 provenance 解析),
    保证谓词与 M1 谓词表词汇一致;行号取守卫节点自身。
    """
    out: list[StaticCandidate] = []
    for py in sorted(Path(source_dir).rglob("*.py")):
        if "__pycache__" in str(py):
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            out.extend(_guard_candidates(fn, str(py)))
    return out


def _guard_candidates(fn, filename: str) -> list[StaticCandidate]:
    """一个函数内的 session 守卫 → guard 候选(带 If/While 行号)。

    复用 predicate_extractor._resolve_condition(同一 provenance 解析),
    保证候选谓词与 M1 谓词表词汇一致;行号取守卫节点自身。
    """
    prov = SessionProvenance()
    prov.analyze(fn)
    out: list[StaticCandidate] = []
    for node in ast.walk(fn):
        if not isinstance(node, (ast.If, ast.While)):
            continue
        pred = _resolve_condition(node.test, prov)
        if pred is None:
            continue
        op = f"guard:{pred.op.value}"
        if pred.value is not None:
            op = f"guard:{pred.op.value}({pred.value})"
        out.append(StaticCandidate(
            function=fn.name, var=pred.variable.canonical, op=op,
            evidence=[f"{filename}:{node.lineno}"]))
    return out
