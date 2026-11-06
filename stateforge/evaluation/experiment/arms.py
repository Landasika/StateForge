"""Versioned experimental arm definitions; labels are scoped to a suite.

Legacy portfolio and reinjection labels remain in recorded commands. Their
canonical strategy is identical in the current epoch controller.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from types import MappingProxyType


REGISTRY_VERSION = 1


@dataclass(frozen=True)
class ArmSpec:
    mode: str
    use_feedback: bool
    policy: str | None = None
    continuous: bool = False
    injector: str | None = None

    @property
    def canonical_policy(self) -> str:
        return "reinjection" if self.policy in (None, "portfolio") else self.policy

    def epoch_options(self) -> dict:
        options = {"mode": self.mode, "use_feedback": self.use_feedback}
        if self.policy is not None:
            options["policy"] = self.policy
        if self.injector == "goal":
            from stateforge.solving.goal_seed_injector import inject_goal_seeds
            options["state_injector"] = inject_goal_seeds
        return options

    def describe(self) -> dict:
        return {**asdict(self), "canonical_policy": self.canonical_policy}


NATIVE = ArmSpec("off", False, continuous=True)
MEASURE = ArmSpec("measure", False)
STRUCTURAL = ArmSpec("feedback", True)
FRONTIER = ArmSpec("frontier", True)
STATE = ArmSpec("frontier", True, "state")
GOAL = ArmSpec("frontier", True, "state", injector="goal")

RUNNER_ARMS = MappingProxyType({
    "A_native": NATIVE,
    "A_measure": ArmSpec("measure", False, continuous=True),
    "B": MEASURE,
    "C": STRUCTURAL,
    "D1": FRONTIER,
    "D2R": ArmSpec("frontier", True, "reinjection"),
    "D2P": ArmSpec("frontier", True, "portfolio"),
    "E": STATE,
    "F": GOAL,
})
FOUR_ARM_ARMS = MappingProxyType({
    "A": NATIVE, "B": STRUCTURAL,
    "C": ArmSpec("frontier", True, "portfolio"),
    "D": ArmSpec("frontier", True, "llm"),
    "E": STATE, "F": GOAL,
})


def manifest(suite: str, arms) -> dict:
    return {"version": REGISTRY_VERSION, "suite": suite,
            "arms": {label: spec.describe() for label, spec in arms.items()}}
