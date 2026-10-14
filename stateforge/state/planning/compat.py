"""compat.py —— 应用特定规则兼容层(诊断 4 隔离)。

内容与边界:
  JSON_FILL             vulnshop 族 JSON 槽位预填值(coupon_code 等)
  uses_token()          计划是否消费 ?token 槽
  ensure_auth()         认证链完备化:用 token 而缺 login/register 时自动补
  resource_completion() "?oid 等资源槽无上游 extract → 前插资源创建端点

这些是**既有靶场(vulnshop 族)的实用规则**,不是通用规划算法的一部分;
planner 核心(plan_backward)只经本模块调用,应用特定知识不得散落通用
路径(消融臂 state_ablation 按"无本层"口径对照,贡献差即本层+链选择)。
通用机制(路由参数/数据流/响应绑定自动生成请求)按 Phase-3 排期替换,
届时本模块退役或降级为 vulnshop 专用 adapter。
"""

from __future__ import annotations

from stateforge.core.model import AppModel, PlanStep
from stateforge.state.planning.goals import GoalSpec, auth_provisioning, register_step

JSON_FILL = {
    "username": "@fresh:u",
    "password": "@fresh:pw",
    "items": "@items",
    "coupon_code": "SAVE10",
    "amount": "100",
    "role": "admin",
}


def uses_token(steps: list[PlanStep]) -> bool:
    """精确槽位匹配:'?token' 不匹配 '?token_a'(双演员场景)。"""
    return any(
        s.headers.get("Authorization", "").split()[-1:] == ["?token"] for s in steps
    )


def produces_token(steps: list[PlanStep]) -> bool:
    return any(v == "?token" for s in steps for v in s.extract.values())


def ensure_auth(steps: list[PlanStep]) -> list[PlanStep]:
    """链完备化:有 token 使用但缺 login/register 时补认证链。"""
    if not uses_token(steps):
        return steps
    has_login = any(s.action.endswith("/auth/login") for s in steps)
    has_register = any(s.action.endswith("/auth/register") for s in steps)
    if has_login and has_register:
        return steps
    if has_login and not has_register:
        return [register_step()] + steps
    return auth_provisioning() + steps


def resource_completion(model: AppModel, ep_index: dict, goal: GoalSpec,
                        steps: list[PlanStep],
                        to_step) -> list[PlanStep]:
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
            create = to_step(ep_index, f"POST {ep.route}", goal)
            # 插到 login 之后(create 自身需要 ?token);无 login 则最前
            insert_at = 0
            for i, s in enumerate(steps):
                if s.action.endswith("/auth/login"):
                    insert_at = i + 1
            return steps[:insert_at] + [create] + steps[insert_at:]
    return steps
