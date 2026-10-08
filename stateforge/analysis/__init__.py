"""Static structure analysis domain (analyzer/static/graph merged).

ProgramIR 之上的静态结构分析:CFG/ICFG(analysis/static/)、语义分析入口
(analysis/analyzer.py)、SSTG 结构图(analysis/builder.py、analysis/export.py)。
"""

from stateforge.analysis.static.icfg import build_icfg

__all__ = ["build_icfg"]
