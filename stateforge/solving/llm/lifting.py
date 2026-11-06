"""Compatibility module; implementation lives in analysis.semantics.lifting."""

import sys
from stateforge.analysis.semantics import lifting as _implementation

# Preserve module identity so legacy cache/Provider monkeypatches remain valid.
sys.modules[__name__] = _implementation
