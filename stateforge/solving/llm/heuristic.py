"""Compatibility module; implementation lives in analysis.semantics.heuristic."""

import sys
from stateforge.analysis.semantics import heuristic as _implementation

# Preserve module identity so legacy cache/Provider monkeypatches remain valid.
sys.modules[__name__] = _implementation
