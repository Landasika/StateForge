"""Language-neutral call-graph solving.

The solver consumes :mod:`stateforge.analysis.facts` and answers "which
function does this call site target?".  It contains no parser and no
language-specific syntax knowledge, so a new frontend inherits the same
evidence ordering and ambiguity policy for free.
"""

from stateforge.analysis.solver.callgraph import NameCallSolver

__all__ = ["NameCallSolver"]
