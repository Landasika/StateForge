"""Runtime domain (dynamic/executor/snapshot merged).

运行时域:轨迹采集与静态映射(runtime/tracer.py、mapper.py、coverage.py、io.py)、
轨迹回放(runtime/replay.py、snapshot_replay.py)、快照子包(runtime/snapshot/)。
"""

from stateforge.runtime.tracer import TraceEvent, TraceSession
from stateforge.runtime.mapper import MappedTrace, map_trace
from stateforge.runtime.coverage import Coverage, coverage

__all__ = ["TraceEvent", "TraceSession", "MappedTrace", "map_trace",
           "Coverage", "coverage"]
