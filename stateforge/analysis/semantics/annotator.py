"""Semantic annotation backend, separate from framework fact extraction."""

from __future__ import annotations

from stateforge.analysis.selector import select
from stateforge.analysis.semantics.lifting import label, relate, apply_lift_result
from stateforge.analysis.semantics.provider import Provider
from stateforge.core.model import AppModel


class SemanticAnnotator:
    def __init__(self, provider: Provider | None = None):
        self.provider = provider

    @classmethod
    def from_env(cls) -> "SemanticAnnotator":
        return cls(Provider.from_env())

    def __call__(self, model: AppModel, domains: dict[str, list[str]]) -> None:
        state_vars, _ = label(model.candidates, domains, provider=self.provider)
        model.state_vars = select(state_vars)
        relevant = [sv for sv in model.state_vars if sv.security_relevant]
        if relevant:
            lift = relate(relevant, model.endpoints, provider=self.provider)
            apply_lift_result(model, lift)
