"""replay.py —— SF2-T3 提前(决策 B):规划链确定性重放执行器。

httpx WSGITransport 进程内直调(无 HTTP 栈、无端口——Atropos"剔除 HTTP"教训的
正确性版);快照模式执行器在 SF2 接管,接口不变。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from stateforge.core.model import PlanSegment, PlanStep
from stateforge.state.planning.binding import BindingContext
from stateforge.state.planning.goals import GoalSpec


@dataclass
class StepTrace:
    step: PlanStep
    route: str
    status: int
    body: Any = None


@dataclass
class GoalResult:
    goal: str
    hit: bool
    reason: str = ""
    steps: list[StepTrace] = field(default_factory=list)
    final_state: dict = field(default_factory=dict)


class ReplayExecutor:
    """两种传输:进程内 WSGI(单测)| 真 HTTP 直跑(SF3-T5 长驻目标联调)。"""

    def __init__(self, wsgi_app=None, salt: str | None = None, base_url: str | None = None):
        if base_url:
            self.client = httpx.Client(base_url=base_url, timeout=10.0)
        elif wsgi_app is not None:
            self.client = httpx.Client(
                transport=httpx.WSGITransport(app=wsgi_app), base_url="http://replay"
            )
        else:
            raise ValueError("wsgi_app 或 base_url 必须给一个")
        self.salt = salt

    def run_segment(self, segment: PlanSegment, goal: GoalSpec,
                    checker: Callable[..., GoalResult] | None = None,
                    budget_ms: float = 10000.0) -> GoalResult:
        ctx = BindingContext(salt=self.salt)
        result = GoalResult(goal=segment.goal, hit=False)
        for step in segment.steps:
            route, json_body, headers = ctx.render_step(step)
            json_body = {k: _coerce(v) for k, v in (json_body or {}).items()}
            method = step.action.partition(" ")[0].lower()
            resp = self.client.request(method, route, json=json_body or None, headers=headers)
            body = _safe_json(resp)
            result.steps.append(StepTrace(step, route, resp.status_code, body))
            ctx.absorb(step.extract, body)
            if resp.status_code >= 400:
                result.reason = f"blocked at {step.action} → {resp.status_code}"
                return result
        if checker is not None:
            from stateforge.state.planning.checkers import verify_request

            vreq = verify_request(segment.goal, ctx)
            verify = (None, {}) if vreq is None else self.get(
                vreq["route"], vreq.get("headers", {}).get("Authorization", "").replace("Bearer ", "") or None
            )
            return checker(result, ctx, verify)
        result.hit = True  # 无 checker:链走通即达
        result.reason = "chain completed"
        return result

    def get(self, route: str, token: str | None = None) -> tuple[int, Any]:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        resp = self.client.get(route, headers=headers)
        return resp.status_code, _safe_json(resp)


def _safe_json(resp) -> Any:
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return resp.text


def _coerce(value):
    """JSON 体里的纯数字字符串还原为 int(计划模板的参数覆写是字符串形态)。"""
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return value
