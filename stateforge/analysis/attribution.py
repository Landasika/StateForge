"""路由反向归属(Route Attribution):函数 → 能到达它的路由集合。

调度器的关键一跳:状态机说"缺 ``session[id]``",静态骨架说
"``login_user`` 写它"——**哪条路由能执行到 ``login_user``**?
本模块用 ICFG 的 call 边反向上溯回答。

语义:**may 可达**。静态 call 边是宽松的(为查全率多连、动态剪),
所以"路由 R 归属到函数 F"的含义是"存在一条调用链 R→…→F",
不保证每次执行都到达。对调度器这是正确语义:它要的是候选集,
执行后由观测裁决。

匹配约定:
- 端点 (file, func) ↔ ICFG 函数 qname(``module.func`` /
  ``module.Class.method``):文件基名相等 + qname 以 ``.func`` 结尾,
  或 CBV(端点登记类名,qname 含 ``.Class.``)。
- 文件基名相同的不同文件可能撞名——归并进同一函数的候选集,
  语义仍是 may,不区分。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from stateforge.core.ir import ProgramIR
from stateforge.core.model import Endpoint


def build_block_owner(ir: ProgramIR) -> dict[str, str]:
    """块 ID → 所属函数 qname。"""
    out: dict[str, str] = {}
    for qname, fn in ir.functions.items():
        for b in fn.blocks:
            out.setdefault(b, qname)
    return out


def function_callers(ir: ProgramIR) -> dict[str, set[str]]:
    """函数级反向调用表:callee qname → caller qname 集合。"""
    owner = build_block_owner(ir)
    out: dict[str, set[str]] = {}
    for e in ir.edges:
        if e.kind != "call":
            continue
        caller = owner.get(e.src)
        callee = owner.get(e.dst)
        if caller and callee and caller != callee:
            out.setdefault(callee, set()).add(caller)
    return out


def function_callees(ir: ProgramIR) -> dict[str, set[str]]:
    """函数级正向调用表:caller qname → callee qname 集合。

    路由归属沿这个方向传播:路由 → 视图 → 视图调用的 → ……(may)。
    """
    owner = build_block_owner(ir)
    out: dict[str, set[str]] = {}
    for e in ir.edges:
        if e.kind != "call":
            continue
        caller = owner.get(e.src)
        callee = owner.get(e.dst)
        if caller and callee and caller != callee:
            out.setdefault(caller, set()).add(callee)
    return out


@dataclass
class RouteAttribution:
    """函数 → 路由的反向可达表 + 端点匹配结果。"""

    routes_of: dict[str, set[str]] = field(default_factory=dict)
    matched_endpoints: dict[str, list[str]] = field(default_factory=dict)
    unmatched_endpoints: list[str] = field(default_factory=list)

    def routes_reaching(self, function_qname: str) -> set[str]:
        """能执行到该函数的路由(may)。"""
        return set(self.routes_of.get(function_qname, ()))


def _route_label(ep: Endpoint) -> str:
    method = ep.methods[0] if ep.methods else "GET"
    return f"{method} {ep.route}"


def attribute_routes(ir: ProgramIR,
                     endpoints: list[Endpoint]) -> RouteAttribution:
    """从每个端点的视图函数出发,沿调用边正向 BFS → 函数的路由集合。

    “路由 R 归属函数 F” = 存在调用链 R的视图 →…→ F(may)。
    """
    callees = function_callees(ir)
    by_basename: dict[str, list[str]] = {}
    for qname, fn in ir.functions.items():
        by_basename.setdefault(Path(fn.file).name, []).append(qname)

    result = RouteAttribution()
    for ep in endpoints:
        base = Path(ep.file).name
        roots: list[str] = []
        for qname in by_basename.get(base, ()):
            if (qname == ep.func or qname.endswith("." + ep.func)
                    or f".{ep.func}." in qname):
                roots.append(qname)
        if not roots:
            result.unmatched_endpoints.append(_route_label(ep))
            continue
        result.matched_endpoints[_route_label(ep)] = roots
        label = _route_label(ep)
        seen: set[str] = set(roots)
        queue: deque[str] = deque(roots)
        while queue:
            fn = queue.popleft()
            result.routes_of.setdefault(fn, set()).add(label)
            for callee in callees.get(fn, ()):
                if callee not in seen:
                    seen.add(callee)
                    queue.append(callee)
    return result
