"""观测状态机:由 ObservedTransition 累积而成。

静态骨架(sites/graph)回答"谁动哪个变量";本模块把观测转移叠成
真正的状态机:节点 = 状态(key 存在性抽象),边 = 触发请求。

查询接口(规划入口):``triggers_producing(key)`` —— 哪些请求能把
某个 key 从无变有。配合静态骨架的 reads 边(哪个函数读这个 key),
即得"要到 X 代码,先打 Y 请求"。

分层(L1.5 state):仅 stdlib + dataclass。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from stateforge.state.join import ObservedTransition

_EMPTY = "∅"


@dataclass(frozen=True)
class MachineEdge:
    from_state: frozenset[str]
    to_state: frozenset[str]
    trigger: str
    points: tuple[str, ...]         # "file:line op slot"(静态归因,证据串)
    changed: tuple[str, ...]        # 值变化但存在性未变的 key
    hits: int = 1


def _state_label(s: frozenset[str]) -> str:
    return _EMPTY if not s else "{" + ",".join(sorted(s)) + "}"


class StateMachine:
    """累积观测转移;按 (from, to, trigger) 去重,重复命中计 hits。"""

    def __init__(self) -> None:
        self._edges: dict[tuple[frozenset, frozenset, str], MachineEdge] = {}

    def add(self, t: ObservedTransition) -> bool:
        """并入一条观测转移;返回 True=新边,False=已有边(hits+1)。"""
        key = (t.from_state, t.to_state, t.trigger)
        if key in self._edges:
            e = self._edges[key]
            points = tuple(dict.fromkeys(e.points + tuple(
                f"{p.file.rsplit('/', 1)[-1]}:{p.line} {p.op} {p.slot}"
                for p in t.points)))
            self._edges[key] = MachineEdge(
                from_state=e.from_state, to_state=e.to_state,
                trigger=e.trigger, points=points,
                changed=tuple(dict.fromkeys(e.changed + t.delta.changed)),
                hits=e.hits + 1)
            return False
        self._edges[key] = MachineEdge(
            from_state=t.from_state, to_state=t.to_state,
            trigger=t.trigger,
            points=tuple(f"{p.file.rsplit('/', 1)[-1]}:{p.line} "
                         f"{p.op} {p.slot}" for p in t.points),
            changed=tuple(t.delta.changed))
        return True

    @property
    def edges(self) -> list[MachineEdge]:
        return list(self._edges.values())

    @property
    def states(self) -> set[frozenset[str]]:
        out: set[frozenset[str]] = set()
        for e in self._edges.values():
            out.add(e.from_state)
            out.add(e.to_state)
        return out

    def triggers_producing(self, key: str) -> list[MachineEdge]:
        """哪些触发请求把 ``key`` 从无变有(规划:先打谁)。"""
        return [e for e in self._edges.values()
                if key in e.to_state and key not in e.from_state]

    def to_dict(self) -> dict:
        return {
            "states": [_state_label(s) for s in sorted(
                self.states, key=lambda s: (len(s), sorted(s)))],
            "edges": [{
                "from": _state_label(e.from_state),
                "to": _state_label(e.to_state),
                "trigger": e.trigger,
                "points": list(e.points),
                "changed": list(e.changed),
                "hits": e.hits,
            } for e in self._edges.values()],
        }

    def to_dot(self, title: str = "observed state machine") -> str:
        lines = [f'digraph "{title}" {{',
                 "  rankdir=LR;",
                 '  node [fontname="Helvetica", shape=ellipse];']
        for s in self.states:
            lines.append(f'  "{_state_label(s)}";')
        for e in self._edges.values():
            label = e.trigger
            if e.changed:
                label += f" (~{','.join(sorted(e.changed))})"
            attrs = [f'label="{label}"']
            if not e.from_state:
                attrs.append("color=green")       # 从空态出发(入口)
            lines.append(f'  "{_state_label(e.from_state)}" -> '
                         f'"{_state_label(e.to_state)}"'
                         f' [{", ".join(attrs)}];')
        lines.append("}")
        return "\n".join(lines)
