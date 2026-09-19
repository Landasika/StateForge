"""Skeleton smoke tests: every subpackage imports on the driver-side python.

Driver side = system python (3.13). Anything needing the ANOTA interpreter
carries the ``anota`` marker and never runs here (see pytest.ini).
"""

import importlib

import pytest

SUBPACKAGES = [
    "stateforge.core",
    "stateforge.analyzer",
    "stateforge.graph",
    "stateforge.planner",
    "stateforge.fuzz",
    "stateforge.executor",
    "stateforge.snapshot",
    "stateforge.oracle",
    "stateforge.llm",
    "stateforge.eval",
    "stateforge.targets",
]


@pytest.mark.parametrize("name", SUBPACKAGES)
def test_subpackage_imports(name: str) -> None:
    importlib.import_module(name)
