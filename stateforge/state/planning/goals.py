"""goals.py —— SF3-T1 GoalSpec 库:每漏洞类一个目标模板。

三类 kind(SRT 的 Goal 层;Oracle 在 SF4):
  reach_state          到达某状态原子(如 Order.status=SHIPPED)
  forbidden_transition 到达迁移终态且路径上 from 态从未成立(bypass)
  authz_replay         A 建资源 → B 重放(跨用户)
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from stateforge.core.model import PlanStep


class ActorSpec(BaseModel):
    role: Literal["any_user", "other_user", "admin", "anonymous"] = "any_user"
    fresh_identity: bool = True


class GoalSpec(BaseModel):
    name: str
    vuln_class: str
    kind: Literal["reach_state", "forbidden_transition", "authz_replay"]
    target: str | None = None                 # 状态原子 "Order.status=SHIPPED"
    transition: tuple[str, str] | None = None  # forbidden: (from, to)
    actor: ActorSpec = ActorSpec()
    max_depth: int = 8
    verify_endpoint: str | None = None
    # 攻击模式:执行时假设这些前置在运行时缺席(静态缺失 ≠ 运行时可达)
    drop_requires: list[str] = Field(default_factory=list)
    # 参数覆写(如 payment_bypass 的 amount=10)
    param_overrides: dict[str, str] = Field(default_factory=dict)
    # 自定义步骤模板(特殊 goal:coupon_reuse 三连、authz_replay 双演员)
    step_template: list[PlanStep] = Field(default_factory=list)


ITEMS = [{"sku": "book", "price": 100, "qty": 1}]

VULNSHOP_GOALS: list[GoalSpec] = [
    GoalSpec(
        name="goal_workflow_bypass", vuln_class="workflow_bypass",
        kind="forbidden_transition", target="Order.status=SHIPPED",
        transition=("PAID", "SHIPPED"),
        drop_requires=["Order.status"],       # 攻击假设:ship 的 PAID 前置运行时缺席
        verify_endpoint="GET /orders/<int:oid>",
    ),
    GoalSpec(
        name="goal_coupon_reuse", vuln_class="coupon_reuse",
        kind="reach_state", target="Coupon.used=1",
        step_template=[
            PlanStep(action="POST /auth/register", json={"username": "@fresh:u", "password": "@fresh:pw"}),
            PlanStep(action="POST /auth/login", json={"username": "@fresh:u", "password": "@fresh:pw"},
                     extract={"token": "?token"}),
            PlanStep(action="POST /orders", json={"items": "@items", "coupon_code": "SAVE10"},
                     headers={"Authorization": "Bearer ?token"}, extract={"id": "?oid1"}),
            PlanStep(action="POST /orders", json={"items": "@items", "coupon_code": "SAVE10"},
                     headers={"Authorization": "Bearer ?token"}, extract={"id": "?oid2"}),
        ],
        verify_endpoint="GET /coupons/<code>",
    ),
    GoalSpec(
        name="goal_idor", vuln_class="idor",
        kind="authz_replay", target="Order.status=CREATED",
        step_template=[
            # A:建资源
            PlanStep(action="POST /auth/register", json={"username": "@fresh:a", "password": "@fresh:pw"}),
            PlanStep(action="POST /auth/login", json={"username": "@fresh:a", "password": "@fresh:pw"},
                     extract={"token": "?token_a"}),
            PlanStep(action="POST /orders", json={"items": "@items"},
                     headers={"Authorization": "Bearer ?token_a"}, extract={"id": "?oid"}),
            # B:独立身份重放 A 的读
            PlanStep(action="POST /auth/register", json={"username": "@fresh:b", "password": "@fresh:pw"}),
            PlanStep(action="POST /auth/login", json={"username": "@fresh:b", "password": "@fresh:pw"},
                     extract={"token": "?token_b"}),
            PlanStep(action="GET /orders/<int:oid>", path={"oid": "?oid"},
                     headers={"Authorization": "Bearer ?token_b"}),
        ],
        verify_endpoint="GET /orders/<int:oid>",
    ),
    GoalSpec(
        name="goal_payment_bypass", vuln_class="payment_bypass",
        kind="reach_state", target="Order.status=PAID",
        param_overrides={"amount": "10"},      # 只付 10(total=100)仍要 PAID
        verify_endpoint="GET /orders/<int:oid>",
    ),
    GoalSpec(
        name="goal_privilege", vuln_class="privilege",
        kind="authz_replay", target="User.role=user",
        step_template=[
            # register/login 由完备化规则自动前置(此处只写攻击本体)
            PlanStep(action="GET /auth/me", headers={"Authorization": "Bearer ?token"},
                     extract={"id": "?uid"}),
            PlanStep(action="POST /admin/users/<int:uid>/role", json={"role": "admin"},
                     headers={"Authorization": "Bearer ?token"}, path={"uid": "?uid"}),
        ],
        verify_endpoint="GET /auth/me",
    ),
]


def auth_provisioning(prefix: str = "u") -> list[PlanStep]:
    """认证链预备步(register → login → ?token);replay 引擎对 authed goal 自动前置。"""
    return [
        PlanStep(action="POST /auth/register",
                 json={"username": f"@fresh:{prefix}", "password": "@fresh:pw"}),
        PlanStep(action="POST /auth/login",
                 json={"username": f"@fresh:{prefix}", "password": "@fresh:pw"},
                 extract={"token": "?token"}),
    ]


def register_step(prefix: str = "u") -> PlanStep:
    return PlanStep(action="POST /auth/register",
                    json={"username": f"@fresh:{prefix}", "password": "@fresh:pw"})
