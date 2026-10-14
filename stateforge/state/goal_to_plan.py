"""goal_to_plan.py —— Phase 2/3 桥接:StateGoal → 合成 AppModel → 计划。

一条龙(无模板、无应用特定规则;兼容层 compat.py 不参与):
  未覆盖目标块 ─goal_derivation→ StateGoal(约束集)
      └ 事实按 op 分派臂聚类 → 合成 AppModel(actions=ops:
        CONTROL 读 → Precondition,WRITE → Effect,
        行写字典键 → 字段级 Effect 使字段约束可回溯)
      ─analysis.builder→ SSTG(produces/requires)
      ─planning.backward→ PlanSegment(动作链)

值形约定(链闭合关键):
  EXISTS/truthy 前置 → atom "<var>=True",对应整行写/Session 写
  Effect value="True";EQ 动态值前置 → atom "<var>=*",对应整行写的
  字典键 Effect value="*"。

适用面(MVP,诚实边界):op 分派式靶(dict-store/微靶词汇);
Flask/Django 路由靶走 patterns.analyze 的原生 AppModel,不经此桥。
"""

from __future__ import annotations

import ast
from pathlib import Path

from stateforge.analysis.builder import build_sstg
from stateforge.core.model import AppModel, Effect, Endpoint, Precondition
from stateforge.state.extractor import (StateFactCandidate, StateOperation,
                                        _store_table)
from stateforge.state.goal_derivation import StateGoal, derive_state_goal
from stateforge.state.planning.backward import plan_backward
from stateforge.state.planning.goals import GoalSpec


def dispatch_arms(source_file: str) -> list[tuple[str, int, int]]:
    """`elif op == "name":` 分派臂 → [(name, start, end)]。

    start=臂首(test 行),end=下一分派臂前一行(函数尾兜底)。
    仅识别与常量的 == 比较(微靶 op 分派惯用法)。
    """
    try:
        tree = ast.parse(Path(source_file).read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return []
    arms: list[tuple[str, int]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if not (isinstance(node, (ast.If, ast.IfExp))
                    and isinstance(node.test, ast.Compare)):
                continue
            t = node.test
            if (len(t.ops) == 1 and isinstance(t.ops[0], ast.Eq)
                    and isinstance(t.left, ast.Name) and t.left.id == "op"
                    and isinstance(t.comparators[0], ast.Constant)
                    and isinstance(t.comparators[0].value, str)):
                arms.append((t.comparators[0].value, node.lineno))
    arms.sort(key=lambda a: a[1])
    max_line = 0
    try:
        max_line = len(Path(source_file).read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeDecodeError):
        pass
    out: list[tuple[str, int, int]] = []
    for i, (name, start) in enumerate(arms):
        end = (arms[i + 1][1] - 1) if i + 1 < len(arms) else max_line
        out.append((name, start, end))
    return out


def _row_write_dict_keys(fact: StateFactCandidate) -> list[str]:
    """整行写字典字面量的键(`_DB[t][k] = {"owner": ...}` → ["owner"])。"""
    try:
        tree = ast.parse(Path(fact.source_file).read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and node.lineno == fact.lineno
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Subscript)
                and isinstance(node.value, ast.Dict)):
            return [k.value for k in node.value.keys
                    if isinstance(k, ast.Constant)]
    return []


def _arm_of(fact: StateFactCandidate,
            arms: list[tuple[str, int, int]]) -> str | None:
    for name, start, end in arms:
        if start <= fact.lineno <= end:
            return name
    return None


def _same_file(a: str, b: str) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return a == b


def synthesize_appmodel(ir, facts: list[StateFactCandidate],
                        goal: StateGoal) -> AppModel:
    """目标所在文件的事实按 op 臂聚类 → AppModel(actions=ops)。

    - CONTROL 读 → Precondition(EXISTS→truthy;EQ/NE 动态值→eq "*");
    - WRITE → Effect(value:常量直取,动态统一 "*" 使 truthy 原子闭合
      为 "<var>=*";session 写统一 value="True" 匹配 truthy 前置);
    - 整行写的字典键 → 字段级 Effect(value="*")。
    """
    arms = dispatch_arms(goal.file)
    eps: dict[str, Endpoint] = {}

    def _ep(name: str, line: int) -> Endpoint:
        if name not in eps:
            eps[name] = Endpoint(func=f"_handle[{name}]", route=f"op={name}",
                                 methods=["POST"], file=goal.file, line=line)
        return eps[name]

    for f in facts:
        if not _same_file(f.source_file, goal.file):
            continue
        arm = _arm_of(f, arms)
        if arm is None:
            continue
        ep = _ep(arm, arms[0][1])
        if f.operation == StateOperation.WRITE:
            if f.domain.value == "SESSION":
                ep.effects.append(Effect(var=f.object_ref, op="set",
                                         value="True", file=f.source_file,
                                         line=f.lineno))
            else:                                  # db.<table> 整行写
                ep.effects.append(Effect(var=f.object_ref, op="set",
                                         value="True", file=f.source_file,
                                         line=f.lineno))
                for key in _row_write_dict_keys(f):   # 字段级效应(EQ 配 *)
                    ep.effects.append(Effect(
                        var=f"{f.object_ref}.{key}", op="set", value="*",
                        file=f.source_file, line=f.lineno))
        elif (f.operation == StateOperation.READ
              and f.relevance.value == "CONTROL"):
            if f.structured_predicate is None:
                continue
            opv = f.structured_predicate.operator.value
            if opv == "EXISTS":
                ep.preconditions.append(Precondition(
                    var=f.object_ref, op="truthy",
                    file=f.source_file, line=f.lineno))
            elif opv in ("EQ", "NE", "IN", "NOT_IN"):
                ep.preconditions.append(Precondition(
                    var=f.object_ref, op="eq", value="*",
                    file=f.source_file, line=f.lineno))
    return AppModel(app_root=goal.file, framework="synthetic",
                    endpoints=list(eps.values()))


def _goal_atom(goal: StateGoal) -> str:
    """目标原子:约束中 db.* 的比较谓词优先(最深层状态),否则 truthy。"""
    for p in goal.constraints:
        if p.subject.startswith(("db.", "DB")) and p.operator.value != "EXISTS":
            return f"{p.subject}=*"
    for p in reversed(goal.constraints):
        return f"{p.subject}=True"
    return ""


def plan_for_block(ir, facts: list[StateFactCandidate],
                   target_block: str) -> tuple[StateGoal, object] | None:
    """一条龙:目标块 → (StateGoal, PlanSegment)。

    反向链收集状态生产者(register→create_order);终结动作 = 目标块
    所在的 op 臂(消费状态、命中深块的操作,如 ship_order)补在链尾。
    """
    goal = derive_state_goal(ir, facts, target_block)
    if goal is None or not goal.constraints:
        return None
    blk = ir.blocks.get(target_block)
    if blk is None:
        return None
    arms = dispatch_arms(goal.file)
    model = synthesize_appmodel(ir, facts, goal)
    if not model.endpoints:
        return None
    sstg = build_sstg(model)
    atom = _goal_atom(goal)
    spec = GoalSpec(name=f"cover:{target_block.split('::')[-3]}",
                    vuln_class="state", kind="reach_state",
                    target=atom, max_depth=4)
    segment = plan_backward(model, sstg, spec)

    from stateforge.core.model import PlanStep

    final_arm = _arm_of_row(arms, blk.start_line)
    if final_arm and not any(s.action == f"POST op={final_arm}"
                             for s in segment.steps):
        segment.steps.append(PlanStep(action=f"POST op={final_arm}"))
        segment.notes.append(
            f"final-action: {final_arm}(目标块所在臂,消费状态命中深块)")
    return goal, segment


def _arm_of_row(arms: list[tuple[str, int, int]], lineno: int) -> str | None:
    for name, start, end in arms:
        if start <= lineno <= end:
            return name
    return None
