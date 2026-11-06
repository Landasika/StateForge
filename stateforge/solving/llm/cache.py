"""Compatibility module; implementation lives in analysis.semantics.cache."""

import sys
from stateforge.analysis.semantics import cache as _implementation

# Preserve module identity so legacy cache/Provider monkeypatches remain valid.
sys.modules[__name__] = _implementation
