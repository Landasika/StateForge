"""backward.py —— SF3-T2 反向规划:SSTG 上目标状态 → 动作链。

标准反向链接(AND 语义:action 的全部 requires 都要满足):
  agenda 弹出未满足原子 → 找 producers(produces 边)→ 择优收集动作 →
  其缺失 requires 入队 → 翻转为顺序 PlanSegment。

生产者择优(诊断 1):有 produces 边的候选按"未满足前置数 → 总前置数 →
字典序"成本序取优,不盲取第一个;运行时可达性留给 Phase-4 动态验证。

soft producer 兜底(静态提取盲区,如参数化 SQL 的 status=?):
  原子无 producer 时,把"观测该变量 + 带写参数"的端点视为候选生产者,
  步骤带 notes 标记,运行时由 verify 读回裁决(SF3:静态缺失 ≠ 运行时可达)。
  soft 候选不参与择优——无 produces 边,"前置少"不蕴含"更可能产出"。

attack 模式:GoalSpec.drop_requires 声明"假设运行时缺席的前置"(bypass 假设)。

应用特定规则经显式 PlanningPolicy 注入;缺省 generic 不补靶场路由。
历史 vulnshop profile 保留 JSON 预填、认证链和资源补全规则。
"""

from __future__ import annotations

import networkx as nx

from stateforge.core.model import AppModel, PlanSegment, PlanStep
from stateforge.state.planning.goals import GoalSpec
from stateforge.state.planning.policies import PlanningPolicy, policy_for


# Compatibility name. New code uses the public policy/rendering interface.
def _to_step(ep_index: dict, action: str, goal: GoalSpec) -> PlanStep:
    return policy_for(goal.profile).render_step(ep_index, action, goal)


def _pick_producer(producers: list[str], sstg: nx.MultiDiGraph,
                   seen_atoms: set[str], drop: set[str]) -> str:
    """生产者择优(诊断 1):不盲取第一个。

    静态可用的成本序(升序取 min):
      1. 未满足前置数少者优先——步数即预算,已满足/已弃置的前置不再计;
      2. 总前置数少者优先——动作更简单;
      3. 动作名字典序——确定性(配对实验可复现)。
    运行时可达性与覆盖信息此处不可见,失败重规划留给 Phase-4 动态验证。
    """
    def cost(action: str) -> tuple[int, int, str]:
        reqs = [v for _, v, d in sstg.out_edges(action, data=True)
                if d.get("kind") == "requires"]
        unmet = [v for v in reqs
                 if v not in seen_atoms
                 and v.split("=", 1)[0] not in drop]
        return len(unmet), len(reqs), action

    return min(producers, key=cost)


def plan_backward(model: AppModel, sstg: nx.MultiDiGraph, goal: GoalSpec, *,
                  policy: PlanningPolicy | None = None) -> PlanSegment:
    policy = policy if policy is not None else policy_for(goal.profile)
    if goal.step_template:
        steps = policy.prepare_template(list(goal.step_template))
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
        if producers:
            # 诊断 1:有 produces 边的候选按成本序择优,不盲取第一个
            action = _pick_producer(producers, sstg, seen_atoms, drop)
        else:
            soft = _soft_producers(model, atom)
            if not soft:
                notes.append(f"coverage-gap: {atom} 无生产者(analyzer 质量指标)")
                continue
            # soft 兜底走域序(item 级迁移端点优先):无 produces 边的候选
            # 不参与 _pick_producer——"前置少"不蕴含"更可能产出该原子"
            action = soft[0]
            soft_map[atom] = action
            notes.append(f"soft-producer: {atom} ← {action}(静态无 produces 边,运行时 verify 裁决)")
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
    steps = [policy.render_step(ep_index, a, goal) for a in ordered]
    steps = policy.complete(model, ep_index, goal, steps)
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
