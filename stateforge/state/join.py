"""观测转移(动态快照差 × executed 静态转移点)的 join。

三方输入,一个输出:
- ``before`` / ``after``:一个请求前后的 store 快照(session dict)
- ``executed_points``:该请求执行到的静态转移点(StoreSite,
  由 trace 事件按 (file,line) 对账得到——见动态实测)
- ``trigger``:请求标签(方法 + 路径)

输出 ``ObservedTransition``:动态给 from→to(快照差),静态给
points(哪几条语句)。归不上静态点的 key 如实记 unattributed,不猜。

归因规则(按序):
1. 精确:key 相等(常量折叠后)
2. 下划线前缀:动态 key 去一个前导 ``_`` 再试(``_permanent`` ↔
   ``permanent``)。两个主流框架的内部键都是前导下划线约定
   (Django ``_auth_user_id``、Flask ``_permanent``/``_flashes``),
   这是约定匹配,不是框架词典
3. 清库:executed 里有 clear/flush 点时,所有删除归到它
4. 其余 unattributed

分层(L1.5 state):仅 stdlib + dataclass,依赖同包 sites。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from stateforge.state.sites import StoreSite

_CLEAR_OPS = frozenset({"clear", "flush"})


@dataclass(frozen=True)
class SlotDelta:
    """快照差:新增 / 删除 / 值变化的 key。"""

    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[str, ...]

    def __bool__(self) -> bool:
        return bool(self.added or self.removed or self.changed)


def diff(before: dict, after: dict) -> SlotDelta:
    added = tuple(k for k in after if k not in before)
    removed = tuple(k for k in before if k not in after)
    changed = tuple(k for k in after
                    if k in before and before[k] != after[k])
    return SlotDelta(added=added, removed=removed, changed=changed)


@dataclass(frozen=True)
class Attribution:
    """一个变化 key 的静态归因。via ∈ exact|underscore|clear|unattributed。"""

    key: str
    point: StoreSite | None
    via: str


@dataclass(frozen=True)
class ObservedTransition:
    """一条观测转移:动态 from→to + 静态 points + 触发请求。

    状态用 **key 存在性抽象**(frozenset[key]):核心闸门是存在性判断
    (CTFd ``authed`` = ``bool(session.get("id"))``;Django
    ``is_authenticated`` = ``_auth_user_id`` 在不在)。值级抽象留给
    后续(业务状态机需要 db 命名空间)。
    """

    trigger: str
    from_state: frozenset[str]
    to_state: frozenset[str]
    delta: SlotDelta
    points: tuple[StoreSite, ...]        # 被归因用到的转移点(去重)
    attributions: tuple[Attribution, ...]

    @property
    def unattributed(self) -> tuple[str, ...]:
        return tuple(a.key for a in self.attributions
                     if a.point is None)


def attribute(delta: SlotDelta,
              executed_points: Sequence[StoreSite],
              chains: set[str] | None = None) -> tuple[Attribution, ...]:
    """把快照差归因到 executed 转移点。

    ``chains``:快照对应的 store 链集合(如 Flask 代理是 ``{"session"}``;
    Django 视图是 ``{"request.session", "self.request.session"}``)。
    不给则不过滤(快照本就来自单一 store 时等价)。
    """
    pts = [p for p in executed_points
           if chains is None or p.chain in chains]
    by_key: dict[str, list[StoreSite]] = {}
    clears: list[StoreSite] = []
    for p in pts:
        if p.op in _CLEAR_OPS:
            clears.append(p)
        elif p.key is not None:
            by_key.setdefault(p.key, []).append(p)

    def _find(dynamic_key: str) -> tuple[StoreSite | None, str]:
        if dynamic_key in by_key:
            return by_key[dynamic_key][0], "exact"
        stripped = dynamic_key[1:] if dynamic_key.startswith("_") else None
        if stripped and stripped in by_key:
            return by_key[stripped][0], "underscore"
        return None, "unattributed"

    out: list[Attribution] = []
    for k in delta.added:
        p, via = _find(k)
        out.append(Attribution(key=k, point=p, via=via))
    for k in delta.changed:
        p, via = _find(k)
        out.append(Attribution(key=k, point=p, via=via))
    for k in delta.removed:
        if clears:
            out.append(Attribution(key=k, point=clears[0], via="clear"))
        else:
            p, via = _find(k)
            out.append(Attribution(key=k, point=p, via=via))
    return tuple(out)


def observe(trigger: str, before: dict, after: dict,
            executed_points: Sequence[StoreSite],
            chains: set[str] | None = None) -> ObservedTransition | None:
    """一个请求 → 一条观测转移;快照无变化返回 None(如登录失败)。"""
    d = diff(before, after)
    if not d:
        return None
    attributions = attribute(d, executed_points, chains)
    used: dict[int, StoreSite] = {}
    for a in attributions:
        if a.point is not None:
            used[id(a.point)] = a.point
    return ObservedTransition(
        trigger=trigger,
        from_state=frozenset(before),
        to_state=frozenset(after),
        delta=d,
        points=tuple(used.values()),
        attributions=attributions)
