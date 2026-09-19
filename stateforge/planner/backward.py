"""backward.py —— SF3-T2 反向规划:SSTG 上目标状态 → 动作链。

标准反向链接(AND 语义:action 的全部 requires 都要满足):
  agenda 弹出未满足原子 → 找 producers(produces 边)→ 收集动作 → 其缺失
  requires 入队 → 翻转为顺序 PlanSegment。

soft producer 兜底(静态提取盲区,如参数化 SQL 的 status=?):
  原子无 producer 时,把"观测该变量 + 带写参数"的端点视为候选生产者,
  步骤带 notes 标记,运行时由 verify 读回裁决(SF3:静态缺失 ≠ 运行时可达)。

attack 模式:GoalSpec.drop_requires 声明"假设运行时缺席的前置"(bypass 假设)。
"""

from __future__ import annotations

import networkx as nx

from stateforge.core.model import AppModel, PlanSegment, PlanStep
from stateforge.planner.goals import GoalSpec, auth_provisioning, register_step

_JSON_FILL = {
    "username": "@fresh:u",
    "password": "@fresh:pw",
    "items": "@items",
    "coupon_code": "SAVE10",
    "amount": "100",
    "role": "admin",
}


def plan_backward(model: AppModel, sstg: nx.MultiDiGraph, goal: GoalSpec) -> PlanSegment:
    if goal.step_template:
        steps = list(goal.step_template)
        if _uses_token(steps) and not _produces_token(steps):
            steps = auth_provisioning() + steps
        return PlanSegment(goal=goal.name, steps=steps)

    ep_index = {(ep.methods[0], ep.route): ep for ep in model.endpoints}
    drop = set(goal.drop_requires)

    agenda: list[tuple[str, int]] = [(goal.target, goal.max_depth)]
    seen_atoms = {goal.target}
    collected: list[str] = []
    soft_map: dict[str, str] = {}  # atom → soft producer action
    notes: list[str] = []

    while agenda:
        atom, depth = agenda.pop(0)
        if depth <= 0:
            continue
        producers = [
            u for u, _, d in sstg.in_edges(atom, data=True) if d.get("kind") == "produces"
        ]
        if not producers:
            soft = _soft_producers(model, atom)
            if soft:
                soft_map[atom] = soft[0]
                producers = soft
                notes.append(f"soft-producer: {atom} ← {soft[0]}(静态无 produces 边,运行时 verify 裁决)")
            else:
                notes.append(f"coverage-gap: {atom} 无生产者(analyzer 质量指标)")
                continue
        action = producers[0]
        if action not in collected:
            collected.append(action)
        for _, v, d in sstg.out_edges(action, data=True):
            if d.get("kind") != "requires" or v in seen_atoms:
                continue
            var = v.split("=", 1)[0]
            if var in drop:
                notes.append(f"drop-requires: {action} 的 {v} 前置按攻击假设跳过")
                continue
            seen_atoms.add(v)
            agenda.append((v, depth - 1))

    ordered = _topo_order(sstg, collected, soft_map)
    steps = [_to_step(ep_index, a, goal) for a in ordered]
    steps = _ensure_auth(steps)
    steps = _resource_completion(model, ep_index, goal, steps)
    return PlanSegment(goal=goal.name, steps=steps, notes=notes)


def _topo_order(sstg: nx.MultiDiGraph, actions: list[str],
                soft_map: dict[str, str]) -> list[str]:
    """依赖序:动作必须排在其所需原子的生产者(含 soft)之后。"""
    producers_of: dict[str, list[str]] = {}
    for a in actions:
        for _, v, d in sstg.out_edges(a, data=True):
            if d.get("kind") == "produces":
                producers_of.setdefault(v, []).append(a)
    for atom, a in soft_map.items():
        producers_of.setdefault(atom, []).append(a)

    deps: dict[str, set[str]] = {a: set() for a in actions}
    for a in actions:
        for _, v, d in sstg.out_edges(a, data=True):
            if d.get("kind") == "requires":
                for p in producers_of.get(v, []):
                    if p in deps and p != a:
                        deps[a].add(p)

    out: list[str] = []
    remaining = list(actions)
    while remaining:
        ready = [a for a in remaining if not (deps[a] & set(remaining))]
        if not ready:  # 环保险:按收集序放行一个
            ready = remaining[:1]
        for a in ready:
            out.append(a)
            remaining.remove(a)
    return out


def _resource_completion(model: AppModel, ep_index: dict, goal: GoalSpec,
                         steps: list[PlanStep]) -> list[PlanStep]:
    """?oid 等资源槽无上游 extract → 前插资源生产端点(POST 且响应含 id)。"""
    used: set[str] = set()
    for s in steps:
        for v in list(s.path.values()) + list(s.json.values()):
            if isinstance(v, str) and v.startswith("?"):
                used.add(v[1:])
    bound = {slot.lstrip("?") for s in steps for slot in s.extract.values()}
    if not (used - bound):
        return steps
    for ep in model.endpoints:
        if ep.methods[0] == "POST" and "id" in ep.observes and "/auth/" not in ep.route:
            create = _to_step(ep_index, f"POST {ep.route}", goal)
            # 插到 login 之后(create 自身需要 ?token);无 login 则最前
            insert_at = 0
            for i, s in enumerate(steps):
                if s.action.endswith("/auth/login"):
                    insert_at = i + 1
            return steps[:insert_at] + [create] + steps[insert_at:]
    return steps


def _ensure_auth(steps: list[PlanStep]) -> list[PlanStep]:
    """链完备化:有 token 使用但缺 login/register 时补认证链。"""
    if not _uses_token(steps):
        return steps
    has_login = any(s.action.endswith("/auth/login") for s in steps)
    has_register = any(s.action.endswith("/auth/register") for s in steps)
    if has_login and has_register:
        return steps
    if has_login and not has_register:
        return [register_step()] + steps
    return auth_provisioning() + steps


def _soft_producers(model: AppModel, atom: str) -> list[str]:
    """观测该变量且带写动作的端点 = 候选生产者(参数化写的盲区兜底)。

    item 级(带路径参数)端点优先:pay/ship 这类迁移操作在 collection 级
    create 之前。
    """
    var = atom.split("=", 1)[0]
    attr = var.split(".")[1]
    candidates = []
    for ep in model.endpoints:
        if attr in ep.observes and ep.methods[0] in ("POST", "PUT"):
            candidates.append(f"{ep.methods[0]} {ep.route}")
    candidates.sort(key=lambda a: "<" not in a)  # 带路径参数的排前
    return candidates


def _to_step(ep_index: dict, action: str, goal: GoalSpec) -> PlanStep:
    method, _, route = action.partition(" ")
    ep = ep_index.get((method, route))
    step = PlanStep(action=action)
    if ep is None:
        return step
    for p in ep.params:
        if p.location == "path":
            step.path[p.name] = f"?{p.name}"
        elif p.location == "json":
            step.json[p.name] = goal.param_overrides.get(p.name) or _JSON_FILL.get(p.name, "x")
    if any(pre.var == "Session.authed_as" for pre in ep.preconditions):
        step.headers["Authorization"] = "Bearer ?token"
    if route.endswith("/login"):
        step.extract["token"] = "?token"
    elif method == "POST" and "<" not in route and ep.observes and "id" in ep.observes:
        # 只有集合级 POST(无路径参数)才是"资源创建",其响应 id 才是新资源
        step.extract["id"] = "?oid"
    return step


def _uses_token(steps: list[PlanStep]) -> bool:
    """精确槽位匹配:'?token' 不匹配 '?token_a'(双演员场景)。"""
    return any(
        s.headers.get("Authorization", "").split()[-1:] == ["?token"] for s in steps
    )


def _produces_token(steps: list[PlanStep]) -> bool:
    return any(v == "?token" for s in steps for v in s.extract.values())
