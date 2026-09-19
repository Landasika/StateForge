"""checkers.py —— SF3:goal 命中判定(链走通后的状态核验;Oracle 的完整版在 SF4)。

约定:checker(result, ctx, verify),verify=(status, body) 由调用方取回——
  直跑模式(ReplayExecutor):链后独立 GET;
  快照模式(SnapshotReplay):verify 步并入同一 epoch(状态必须还活着)。
blocked(4xx/5xx)在执行器内提前返回,不会进 checker。
"""

from __future__ import annotations

from stateforge.executor.replay import GoalResult
from stateforge.planner.binding import BindingContext


def check_workflow_bypass(result: GoalResult, ctx: BindingContext, verify) -> GoalResult:
    status, body = verify
    hit = status == 200 and body.get("status") == "SHIPPED" and body.get("paid_amount") == 0
    result.final_state = body or {}
    result.hit = hit
    result.reason = (f"SHIPPED 且 paid=0 → bypass {'成立' if hit else '不成立'}"
                     if status == 200 else f"verify {status}")
    return result


def check_payment_bypass(result: GoalResult, ctx: BindingContext, verify) -> GoalResult:
    status, body = verify
    hit = status == 200 and body.get("status") == "PAID" and body.get("paid_amount", 0) < body.get("total", 0)
    result.final_state = body or {}
    result.hit = hit
    result.reason = (f"PAID 但 paid<total → bypass {'成立' if hit else '不成立'}"
                     if status == 200 else f"verify {status}")
    return result


def check_coupon_reuse(result: GoalResult, ctx: BindingContext, verify) -> GoalResult:
    status, body = verify
    both_created = all(t.status == 201 for t in result.steps if t.route == "/orders")
    hit = status == 200 and body.get("used") == 1 and both_created
    result.final_state = body or {}
    result.hit = hit
    result.reason = f"两次建单均 201 且 used 置位 → {'复用成立' if hit else '未成立'}"
    return result


def check_privilege(result: GoalResult, ctx: BindingContext, verify) -> GoalResult:
    status, body = verify
    hit = status == 200 and body.get("role") == "admin"
    result.final_state = body or {}
    result.hit = hit
    result.reason = f"role={body.get('role')} → 提权{'成立' if hit else '不成立'}"
    return result


def check_idor(result: GoalResult, ctx: BindingContext, verify) -> GoalResult:
    """链走通 = B 用自己的 token 读到了 A 的订单(fixed 变体在 403 处已被 blocked)。"""
    last = result.steps[-1]
    result.final_state = last.body if isinstance(last.body, dict) else {}
    result.hit = last.status == 200
    result.reason = f"B 跨用户读单 → {last.status}"
    return result


CHECKERS = {
    "goal_workflow_bypass": check_workflow_bypass,
    "goal_payment_bypass": check_payment_bypass,
    "goal_coupon_reuse": check_coupon_reuse,
    "goal_privilege": check_privilege,
    "goal_idor": check_idor,
}


def verify_request(goal_name: str, ctx: BindingContext) -> dict:
    """goal 的 verify 读回请求(并入链尾,同 epoch 执行)。"""
    if goal_name == "goal_coupon_reuse":
        return {"method": "GET", "route": "/coupons/SAVE10"}
    if goal_name in ("goal_workflow_bypass", "goal_payment_bypass"):
        return {"method": "GET", "route": f"/orders/{ctx.bindings.get('oid')}",
                "headers": {"Authorization": f"Bearer {ctx.bindings.get('token')}"}}
    if goal_name == "goal_privilege":
        return {"method": "GET", "route": "/auth/me",
                "headers": {"Authorization": f"Bearer {ctx.bindings.get('token')}"}}
    return None  # idor:链尾即判定
