"""Explicit application policy for an otherwise generic backward planner."""

from __future__ import annotations

from stateforge.state.planning.rendering import render_plan_step


class PlanningPolicy:
    def render_step(self, ep_index, action, goal):
        return render_plan_step(ep_index, action, goal)

    def prepare_template(self, steps):
        return steps

    def complete(self, model, ep_index, goal, steps):
        return steps


class VulnshopPolicy(PlanningPolicy):
    def render_step(self, ep_index, action, goal):
        from stateforge.state.planning.compat import JSON_FILL
        return render_plan_step(ep_index, action, goal, json_defaults=JSON_FILL)

    def prepare_template(self, steps):
        from stateforge.state.planning.compat import uses_token, produces_token
        from stateforge.state.planning.goals import auth_provisioning
        if uses_token(steps) and not produces_token(steps):
            return auth_provisioning() + steps
        return steps

    def complete(self, model, ep_index, goal, steps):
        from stateforge.state.planning.compat import ensure_auth, resource_completion
        return resource_completion(model, ep_index, goal, ensure_auth(steps),
                                   self.render_step)


def policy_for(profile: str) -> PlanningPolicy:
    if profile == "generic":
        return PlanningPolicy()
    if profile == "vulnshop":
        return VulnshopPolicy()
    raise ValueError(f"unknown planning profile: {profile}")
