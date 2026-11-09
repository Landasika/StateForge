"""Source-language frontends.

A frontend turns source text into :class:`~stateforge.core.ir.ProgramIR`
blocks/edges *and* reports language-neutral facts
(:mod:`stateforge.analysis.facts`).  It never decides call targets itself: that
belongs to :mod:`stateforge.analysis.solver`.

Adding a language therefore means adding one module here that produces:

* per-function control flow as ``BasicBlock``/``FlowEdge``;
* ``FunctionFact`` for every declaration it can see;
* ``CallSiteFact`` for every call expression, with the owning block.

The rest of the pipeline (:mod:`stateforge.runtime.mapper`,
:mod:`stateforge.analysis.solver`, state and goal analysis) is unchanged.
"""

from stateforge.analysis.frontend.python import build_python_icfg

__all__ = ["build_python_icfg"]
