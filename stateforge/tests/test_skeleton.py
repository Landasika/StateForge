"""Skeleton smoke tests: every subpackage imports on the driver-side python.

Driver side = system python (3.13). Anything needing the ANOTA interpreter
carries the ``anota`` marker and never runs here (see pytest.ini).
"""

import importlib

import pytest

SUBPACKAGES = [
    "stateforge.core",
    "stateforge.analysis",
    "stateforge.analysis.static",
    "stateforge.state.planning",
    "stateforge.fuzzing",
    "stateforge.runtime",
    "stateforge.runtime.snapshot",
    "stateforge.adapters",
    "stateforge.solving.llm",
    "stateforge.evaluation",
    "stateforge.targets",
]


@pytest.mark.parametrize("name", SUBPACKAGES)
def test_subpackage_imports(name: str) -> None:
    importlib.import_module(name)
