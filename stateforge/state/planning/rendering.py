"""Public rendering of plan actions and C10 request steps."""

from __future__ import annotations

from stateforge.core.model import PlanStep
from stateforge.state.planning.binding import BindingContext
from stateforge.state.planning.goals import GoalSpec


def render_plan_step(ep_index: dict, action: str, goal: GoalSpec, *,
                     json_defaults=None) -> PlanStep:
    """SSTG action → PlanStep(槽位物化:path→?名,json→覆盖/预填/中性)。

    JSON/form 默认值由调用策略注入;未覆盖槽位中性 "x"。
    """
    json_defaults = json_defaults or {}
    method, _, route = action.partition(" ")
    ep = ep_index.get((method, route))
    step = PlanStep(action=action)
    if ep is None:
        return step
    for p in ep.params:
        if p.location == "path":
            step.path[p.name] = f"?{p.name}"
        elif p.location == "json":
            step.json[p.name] = goal.param_overrides.get(p.name, json_defaults.get(p.name, "x"))
        elif p.location == "form":
            step.form[p.name] = goal.param_overrides.get(p.name, json_defaults.get(p.name, "x"))
    if any(pre.var == "Session.authed_as" for pre in ep.preconditions):
        step.headers["Authorization"] = "Bearer ?token"
    if route.endswith("/login"):
        step.extract["token"] = "?token"
    elif method == "POST" and "<" not in route and ep.observes and "id" in ep.observes:
        # 只有集合级 POST(无路径参数)才是"资源创建",其响应 id 才是新资源
        step.extract["id"] = "?oid"
    return step


def _symbolic(ctx: BindingContext, v):
    """@fresh/@items 代入;?槽与其余值原样(靶侧运行期绑定,§4)。"""
    if isinstance(v, str) and v.startswith("@"):
        return ctx.render(v)
    return v


def render_seed_step(ctx: BindingContext, st: PlanStep) -> dict:
    method, _, route = st.action.partition(" ")
    path = route
    for name, v in st.path.items():
        val = str(_symbolic(ctx, v))
        path = path.replace(f"<int:{name}>", val).replace(f"<{name}>", val)
    step = {"method": method or "GET", "path": path}
    if st.json:
        step["json"] = {k: _symbolic(ctx, v) for k, v in st.json.items()}
    if st.form:
        step["form"] = {k: _symbolic(ctx, v) for k, v in st.form.items()}
    if st.headers:
        step["header"] = {k: _symbolic(ctx, v) for k, v in st.headers.items()}
    if st.extract:
        step["extract"] = dict(st.extract)
    return step


