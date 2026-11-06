"""Shared replay result contracts for execution and goal checkers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from stateforge.core.model import PlanStep


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


