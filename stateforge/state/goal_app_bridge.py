"""goal_app_bridge.py —— 真实应用:目标块 → 既有 AppModel → HTTP 计划链。

Django 框架补全(结构词汇,零应用特定;LLM 无 key/缓存时的兜底):
  视图函数带 @login_required 装饰器 → Precondition(Session.authed_as)
  urls include django.contrib.auth → 框架 LoginView 端点产
  Effect(Session.authed_as)(Django 内置认证链的结构事实)

流程(复用为主,新代码只做映射):
  未覆盖目标块
    → necessary_guard_edges(goal_derivation,方言无关)
    → 守卫/目标块行号邻域匹配 AppModel endpoint → 约束集(preconditions)
    → 目标原子 → build_sstg + plan_backward(既有)→ PlanSegment
  渲染沿用 backward._to_step(path 参数 ?槽 + json 中性填充)。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from stateforge.analysis.builder import build_sstg, _AUTH_ATOM
from stateforge.core.model import AppModel, Effect, Endpoint, Precondition
from stateforge.state.goal_derivation import necessary_guard_edges

_AUTH_VAR = "Session.authed_as"


def _view_decorators(source_root: str | Path) -> dict[str, list[str]]:
    """view 函数名 → 装饰器名表(AST,结构匹配)。"""
    out: dict[str, list[str]] = {}
    root = Path(source_root)
    for py in root.rglob("*.py"):
        if any(part in {".venv", "__pycache__", "node_modules", ".git"}
               for part in py.parts):
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decs = []
            for d in node.decorator_list:
                if isinstance(d, ast.Name):
                    decs.append(d.id)
                elif isinstance(d, ast.Attribute):
                    decs.append(d.attr)
                elif isinstance(d, ast.Call) and isinstance(d.func, ast.Name):
                    decs.append(d.func.id)
                elif isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute):
                    decs.append(d.func.attr)
            if decs:
                out.setdefault(node.name, []).extend(decs)
    return out


def django_auth_completion(model: AppModel,
                           source_root: str | Path) -> AppModel:
    """框架补全:@login_required → 需 auth;内置 auth urls → login 产 auth。

    原地扩展传入模型(返回同对象便于链式);只加不删。
    """
    root = Path(source_root)
    decs = _view_decorators(root)

    def _needs_auth(ep: Endpoint) -> bool:
        fn = (ep.func or "").rsplit(".", 1)[-1].lower()
        return "login_required" in [d.lower() for d in decs.get(fn, [])]

    for ep in model.endpoints:
        if _needs_auth(ep) and not any(p.var == _AUTH_VAR
                                       for p in ep.preconditions):
            ep.preconditions.append(Precondition(
                var=_AUTH_VAR, op="truthy", file=ep.file, line=ep.line))

    has_login_effect = any(
        e.var == _AUTH_VAR and e.op == "set"
        for ep in model.endpoints for e in ep.effects)
    if not has_login_effect and _includes_django_auth_urls(root):
        # Django 内置认证链(结构事实,非应用白名单)
        model.endpoints.append(Endpoint(
            func="django.contrib.auth.LoginView", route="/login/",
            methods=["POST"], params=[
                __import__("stateforge.core.model", fromlist=["Param"]).Param(
                    name="username", location="json"),
                __import__("stateforge.core.model", fromlist=["Param"]).Param(
                    name="password", location="json"),
            ],
            effects=[Effect(var=_AUTH_VAR, op="set", key="username",
                            file=str(root), line=0)],
        ))
    return model


def _includes_django_auth_urls(root: Path) -> bool:
    pat = re.compile(r"django\.contrib\.auth(\.urls)?|LoginView|auth_urls")
    for py in list(root.rglob("urls.py")) + list(root.rglob("*/urls/*.py")):
        if any(part in {".venv", "__pycache__"} for part in py.parts):
            continue
        try:
            if pat.search(py.read_text(encoding="utf-8")):
                return True
        except (OSError, UnicodeDecodeError):
            continue
    return False


def _endpoint_for_block(model: AppModel, ir, block_id: str) -> Endpoint | None:
    """目标块所在函数 → AppModel endpoint(view 函数名匹配)。"""
    blk = ir.blocks.get(block_id)
    if blk is None:
        return None
    fn = blk.function.rsplit(".", 1)[-1]
    for ep in model.endpoints:
        if (ep.func or "").rsplit(".", 1)[-1] == fn:
            return ep
    return None


def app_goal_constraints(model: AppModel, ir,
                         target_block: str) -> list[Precondition]:
    """目标块 + 必要守卫 → AppModel 端点前置约束集。"""
    out: list[Precondition] = []
    seen: set[tuple] = set()

    def _collect(ep: Endpoint | None, near: tuple[int, int] | None):
        if ep is None:
            return
        for p in ep.preconditions:
            key = (p.var, p.op, p.value)
            if key in seen:
                continue
            if near is not None and not (
                    near[0] - 2 <= p.line <= near[1] + 2):
                continue
            seen.add(key)
            out.append(p)

    _collect(_endpoint_for_block(model, ir, target_block), None)
    for g, _kind in necessary_guard_edges(ir, target_block):
        gblk = ir.blocks[g]
        _collect(_endpoint_for_block(model, ir, g),
                 (gblk.start_line, gblk.end_line))
    return out


def goal_atom(constraints: list[Precondition]) -> str | None:
    """目标原子:db 域谓词优先,否则 auth 原子。"""
    for p in constraints:
        if p.var.split(".")[0] not in ("Session",) and p.op in ("eq", "ne", "in"):
            return f"{p.var}={p.value}" if p.value else None
    if any(p.var == _AUTH_VAR for p in constraints):
        return _AUTH_ATOM
    return None


def plan_app_chain(model: AppModel, ir, target_block: str):
    """目标块 → (约束集, PlanSegment | None)。

    final-action:目标块所在 view 的端点(消费状态、命中深块的动作)
    若不在反向链中则补尾(微靶版同语义)。
    """
    from stateforge.core.model import PlanStep
    from stateforge.state.planning.backward import plan_backward
    from stateforge.state.planning.goals import GoalSpec

    constraints = app_goal_constraints(model, ir, target_block)
    if not constraints:
        return constraints, None
    atom = goal_atom(constraints)
    if atom is None:
        return constraints, None
    spec = GoalSpec(name=f"cover:{target_block.split('::')[-3]}",
                    vuln_class="state", kind="reach_state",
                    target=atom, max_depth=4)
    segment = plan_backward(model, build_sstg(model), spec)
    final_ep = _endpoint_for_block(model, ir, target_block)
    if final_ep is not None:
        action = f"{final_ep.methods[0] if final_ep.methods else 'GET'} {final_ep.route}"
        if not any(s.action == action for s in segment.steps):
            segment.steps.append(PlanStep(action=action))
            segment.notes.append(
                f"final-action: {action}(目标块所在视图,消费状态命中深块)")
    return constraints, segment


def derive_and_plan(app_root: str | Path, framework: str, target_block: str,
                    out_path: str | Path) -> dict:
    """两段式驱动(阶段一,主环境):analyze → 补全 → 规划 → 落盘 JSON。"""
    import json as _json

    import logging
    logging.disable(logging.CRITICAL)
    from stateforge.analysis.analyzer import analyze
    from stateforge.analysis.static.icfg import build_icfg

    root = Path(app_root).resolve()
    model = django_auth_completion(analyze(root, framework), root)
    ir = build_icfg(root)
    constraints, segment = plan_app_chain(model, ir, target_block)
    out = {
        "target_block": target_block,
        "app_root": str(root),
        "constraints": [
            {"var": p.var, "op": p.op, "value": p.value}
            for p in constraints],
        "segment": segment.model_dump() if segment is not None else None,
        "notes": list(segment.notes) if segment else [],
    }
    Path(out_path).write_text(_json.dumps(out, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    return out


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("app_root")
    parser.add_argument("framework")
    parser.add_argument("target_block")
    parser.add_argument("--out", default="/tmp/goal_plan.json")
    args = parser.parse_args()
    result = derive_and_plan(args.app_root, args.framework,
                             args.target_block, args.out)
    print("constraints:", result["constraints"])
    print("steps:", [s["action"] for s in
                     (result["segment"] or {}).get("steps", [])])
    print("plan saved:", args.out)
