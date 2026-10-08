"""Phase-4.1B Level 7/8:证据链驱动器——frontier→guard→operand→source→hint
→candidate→真实执行→mapper 判 FRONTIER_CROSSED;支持嵌套前沿迭代。

事件严格分级:HINT_GENERATED / CANDIDATE_GENERATED / CANDIDATE_EXECUTED /
BRANCH_SATISFIED / FRONTIER_CROSSED(最终裁决权属 Phase-1 mapper)。
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path

from stateforge.core.ir import ProgramIR
from stateforge.runtime.mapper import map_trace
from stateforge.runtime.tracer import TraceSession
from stateforge.solving.branch_observer import Observer, load_observed_target
from stateforge.solving.branch_solver import solve
from stateforge.solving.guard_recovery import _TestIndex, recover_guard
from stateforge.solving.input_provenance import (apply_transforms,
                                                     resolve_operand_source)
from stateforge.fuzzing.mutation_hint import MutationHint
from stateforge.fuzzing.feedback.frontier_tracker import FrontierTracker
from stateforge.fuzzing.constraint_mutator import MutationError, apply_hint


class Request:
    """结构化种子 → 目标应用眼里的请求(args/form 为字符串表,json 原生)。"""

    def __init__(self, seed: dict):
        self.args = dict(seed.get("query", {}))
        self.form = dict(seed.get("form", {}))
        self.json = dict(seed.get("json", {}))
        self.headers = dict(seed.get("header", {}))
        self.cookies = dict(seed.get("cookie", {}))
        self.values = {**self.args, **self.form}  # 组合视图(可执行;
        # provenance 侧仍按协议拒判 SOURCE_AMBIGUOUS)
        self.session = {"authenticated": False}   # 中性未认证态(4.2 的领地)


@dataclass
class SolveResult:
    events: list[dict] = field(default_factory=list)
    seeds_executed: list[dict] = field(default_factory=list)
    crossed: list[str] = field(default_factory=list)
    target_reached: bool = False
    final_seed: dict | None = None


def _execute(mod, observer: Observer, session: TraceSession, seed: dict):
    start_ev = len(session.events)
    start_obs = len(observer.records)
    with session.request():
        try:
            mod.run(Request(seed))
        except Exception:                        # noqa: BLE001
            pass
    return (list(session.events[start_ev:]),
            observer.records[start_obs:])


def solve_loop(app_path: str | Path, ir: ProgramIR, seed: dict,
               target_function: str = "prize", max_rounds: int = 6
               ) -> SolveResult:
    app_path = Path(app_path)
    mod, observer = load_observed_target(app_path)
    tracker = FrontierTracker(ir)
    test_index = _TestIndex(ir)
    result = SolveResult()
    target_blocks = {b.id for b in ir.blocks.values()
                     if b.function == target_function}
    session = TraceSession(root=str(app_path.parent))
    session.__enter__()

    def ev(stage, **kw):
        result.events.append({"stage": stage, **kw})

    def run_seed(s: dict):
        result.seeds_executed.append(dict(s))
        ev("CANDIDATE_EXECUTED", round=len(result.seeds_executed) - 1,
           seed=dict(s))
        events, obs = _execute(mod, observer, session, s)
        mapped = map_trace(events, ir)
        tracker.update(set(mapped.block_hits), set(mapped.edge_kinds))
        return obs, mapped

    try:
        current = seed
        for _round in range(max_rounds):
            obs, mapped = run_seed(current)
            result.target_reached = bool(target_blocks & set(mapped.block_hits))
            if result.target_reached:
                result.final_seed = current
                return result
            chosen = None
            for (u, v) in tracker.edge_frontiers:
                guard = recover_guard(ir, u, v, _edge_kind(ir, u, v),
                                      test_index)
                if guard.status == "ok":
                    chosen = ((u, v), guard)
                    break
            if chosen is None:
                ev("HINT_GENERATED", hint_reason="GUARD_UNSUPPORTED")
                result.final_seed = current
                return result
            (u, v), guard = chosen
            frontier_id = f"{u}->{v}"
            round_no = len(result.seeds_executed) - 1
            gobs = next((o for o in reversed(obs)
                         if o.guard_id == guard.guard_id), None)
            if gobs is None:
                ev("HINT_GENERATED", round=round_no, frontier=frontier_id,
                   hint_reason="OPERAND_UNAVAILABLE")
                result.final_seed = current
                return result
            src = resolve_operand_source(app_path, ir.blocks[u].function,
                                         guard.lineno)
            if src.status != "ok":
                ev("HINT_GENERATED", round=round_no, frontier=frontier_id,
                   hint_reason="NOT_SINGLE_INPUT_SOLVABLE", detail=src.status)
                result.final_seed = current
                return result
            raw = {"QUERY": current.get("query", {}),
                   "FORM": current.get("form", {}),
                   "JSON": current.get("json", {}),
                   "HEADER": current.get("header", {}),
                   "COOKIE": current.get("cookie", {})}[src.kind].get(src.key)
            expect = (apply_transforms(raw, src.transforms)
                      if raw is not None and src.transforms else raw)
            input_is_lhs = _input_is_lhs(app_path, ir.blocks[u].function,
                                         guard.lineno, src.var)
            runtime_val = gobs.lhs if input_is_lhs else gobs.rhs
            if expect != runtime_val:
                ev("HINT_GENERATED", round=round_no, frontier=frontier_id,
                   hint_reason="VALUE_MISMATCH")
                result.final_seed = current
                return result
            hint = solve(frontier_id, guard.guard_id, guard.polarity,
                         guard.op, input_is_lhs, gobs, src.kind, src.key)
            hint = MutationHint(hint.frontier_id, hint.guard_id,
                                hint.source_kind, hint.source_key,
                                hint.current_value, hint.target_value,
                                hint.operator, hint.desired_polarity,
                                hint.reason, hint.confidence,
                                transform_chain=src.transforms)
            ev("HINT_GENERATED", round=round_no, frontier=frontier_id,
               guard=guard.expression, hint_reason=hint.reason,
               target=hint.target_value, source=f"{src.kind}[{src.key}]")
            if hint.reason != "solved":
                result.final_seed = current
                return result
            try:
                candidate = apply_hint(current, hint)
            except MutationError as exc:
                ev("CANDIDATE_GENERATED", round=round_no, error=str(exc))
                result.final_seed = current
                return result
            ev("CANDIDATE_GENERATED", round=round_no,
               candidate=dict(candidate),
               changed_field=f"{src.kind}[{src.key}]")
            # 立即执行候选:BRANCH_SATISFIED(观测)+ FRONTIER_CROSSED(mapper)
            obs2, mapped2 = run_seed(candidate)
            g2 = next((o for o in reversed(obs2)
                       if o.guard_id == guard.guard_id), None)
            if g2 is not None:
                ev("BRANCH_SATISFIED", round=round_no, guard=guard.guard_id,
                   actual=bool(g2.actual),
                   satisfied=bool(g2.actual) == (guard.polarity == "true"))
            if (u, v) in set(mapped2.edge_kinds):
                ev("FRONTIER_CROSSED", round=round_no, frontier=frontier_id)
                result.crossed.append(frontier_id)
            current = candidate
        result.final_seed = current
        return result
    finally:
        session.__exit__(None, None, None)


def _edge_kind(ir: ProgramIR, u: str, v: str) -> str:
    for e in ir.edges:
        if e.src == u and e.dst == v:
            return e.kind
    return "true"


def _input_is_lhs(app_path: Path, function: str, lineno: int,
                  var: str) -> bool:
    import ast as _ast
    tree = _ast.parse(Path(app_path).read_text(encoding="utf-8"))
    for stmt in _ast.walk(tree):
        if isinstance(stmt, (_ast.If, _ast.While)) \
                and getattr(stmt, "test", None) is not None \
                and stmt.test.lineno == lineno \
                and isinstance(stmt.test, _ast.Compare):
            return isinstance(stmt.test.left, _ast.Name) \
                and stmt.test.left.id == var
    return True
