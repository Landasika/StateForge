"""Compatibility module; implementation lives in analysis.semantics.provider."""

import sys
from stateforge.analysis.semantics import provider as _implementation

# Preserve module identity so legacy cache/Provider monkeypatches remain valid.
sys.modules[__name__] = _implementation
