"""Phase-4.2-H:增强 Provenance = ②跨函数静态追踪 + ③动态值关联。

② 静态:provenance 在函数内失败时,从 ICFG call 边找到 caller,
   在 caller 中追踪参数表达式回到输入源。
③ 动态:守卫执行时的操作数值 vs 种子字段值——值匹配即确认关联
   (不依赖代码模式,穿透一切间接层)。

优先级:函数内 provenance > ②跨函数 > ③值关联 > SOURCE_UNKNOWN。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from stateforge.core.ir import ProgramIR
from stateforge.feasibility.input_provenance import (
    InputSource, _extract, _mentions_request, _parse_chain, _test_at)

# ── ② 跨函数静态追踪 ──


def resolve_cross_function(ir: ProgramIR, app_path: Path,
                           callee_function: str,
                           param_name: str) -> InputSource | None:
    """helper 参数 → caller 的实参表达式 → provenance。

    利用 ICFG 的 call 边找到 caller,在 caller 源码中定位调用点,
    追踪对应位置的实参表达式。
    """
    # 找到 callee 的 entry 块
    callee_entry = None
    for b in ir.blocks.values():
        if b.function == callee_function and b.kind == "entry":
            callee_entry = b.id
            break
    if not callee_entry:
        return None

    # 从 ICFG 找 caller(call 边指向 callee entry)
    callers: list[tuple[str, int]] = []      # (caller函数, callsite_line)
    for e in ir.edges:
        if e.kind == "call" and e.dst == callee_entry:
            caller_block = ir.blocks.get(e.src)
            if caller_block:
                callers.append((caller_block.function,
                                e.callsite_line or 0))
    if not callers:
        return None

    # 解析 caller 源码,找调用语句,提取第 N 个实参表达式
    try:
        tree = ast.parse(Path(app_path).read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None

    for caller_fn, callsite_line in callers[:3]:     # 最多追 3 个 caller
        fn_node = _find_function(tree, caller_fn)
        if fn_node is None:
            continue
        arg_expr = _find_call_arg(tree, fn_node, callee_function,
                                  param_name, callsite_line)
        if arg_expr is None:
            continue
        # 实参是 Name → 在 caller 中找该变量的赋值,再跑模式匹配
        if isinstance(arg_expr, ast.Name):
            arg_expr = _resolve_var_in_fn(fn_node, arg_expr.id)
            if arg_expr is None:
                continue
        # 在实参表达式上跑 provenance 模式匹配
        source = _expr_to_source(arg_expr)
        if source:
            source.var = param_name
            return source
    return None


def _resolve_var_in_fn(fn: ast.FunctionDef, var: str) -> ast.expr | None:
    """在函数中找 var 的赋值,返回 RHS 表达式(最后一个)。"""
    result = None
    for stmt in ast.walk(fn):
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == var:
                    result = stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            if isinstance(stmt.target, ast.Name) and stmt.target.id == var:
                result = stmt.value
    return result


def _find_function(tree: ast.Module, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    return None


def _find_call_arg(tree: ast.Module, fn: ast.FunctionDef, callee: str,
                   param: str, near_line: int) -> ast.expr | None:
    """在 caller 中找 callee(...) 调用,返回 param 对应位置的实参。"""
    # 先从 callee 定义拿参数位置
    callee_fn = _find_function(tree, callee)
    if callee_fn is None:
        return None
    param_names = [a.arg for a in callee_fn.args.args]
    if param not in param_names:
        return None
    param_index = param_names.index(param)

    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        callee_name = None
        if isinstance(func, ast.Name):
            callee_name = func.id
        elif isinstance(func, ast.Attribute):
            callee_name = func.attr
        if callee_name != callee:
            continue
        # keyword 优先
        for kw in node.keywords:
            if kw.arg == param:
                return kw.value
        # 位置参数:按 callee 签名中的索引取
        if param_index < len(node.args):
            return node.args[param_index]
    return None


def _expr_to_source(expr) -> InputSource | None:
    """在表达式上跑 provenance 模式匹配(request.args/strip/int 等)。"""
    if isinstance(expr, list):
        for e in expr:
            r = _expr_to_source(e)
            if r:
                return r
        return None
    chain, base = _parse_chain(expr)
    hit = _extract(base)
    if hit is None or hit[0] == "AMBIGUOUS":
        return None
    kind, key = hit
    transform = "->".join(chain) if chain else "identity"
    return InputSource(kind, key, transform, "", transforms=tuple(chain))


# ── ③ 动态值关联 ──


@dataclass
class ValueMatch:
    kind: str                 # QUERY / FORM / JSON / HEADER / COOKIE
    key: str
    field_value: object       # 种子里的原始值
    guard_value: object       # 守卫操作数的运行时值
    transform: str            # "identity" / "int" / "strip" 等
    confidence: float         # 1.0=精确匹配, 0.7=经变换匹配


def correlate_values(guard_operands: list[object],
                     seed: dict) -> list[ValueMatch]:
    """③ 守卫操作数值 vs 种子字段值——值匹配即关联。

    不看代码模式;只看"输入的这个值,到了守卫那里还是不是这个值
    (或经简单变换后的值)"。
    """
    matches = []
    sections = {"QUERY": "query", "FORM": "form", "JSON": "json",
                "HEADER": "header", "COOKIE": "cookie"}
    for kind, section in sections.items():
        fields = seed.get(section, {})
        if not isinstance(fields, dict):
            continue
        for key, field_val in fields.items():
            for operand in guard_operands:
                if operand is None:
                    continue
                # 直接匹配
                if _values_equal(field_val, operand):
                    matches.append(ValueMatch(
                        kind, key, field_val, operand, "identity", 1.0))
                    continue
                # int 变换
                if isinstance(operand, (int, float)) \
                        and isinstance(field_val, str):
                    try:
                        if float(field_val) == float(operand):
                            matches.append(ValueMatch(
                                kind, key, field_val, operand, "int", 0.8))
                    except ValueError:
                        pass
                # strip 变换
                if isinstance(operand, str) and isinstance(field_val, str):
                    if field_val.strip() == operand:
                        matches.append(ValueMatch(
                            kind, key, field_val, operand, "strip", 0.8))
    return matches


def _values_equal(a, b) -> bool:
    if type(a) is type(b):
        return a == b
    return False


# ── 组合入口 ──


def resolve_enhanced(ir: ProgramIR, app_path: Path,
                     function_name: str, guard_lineno: int,
                     guard_operands: list[object] | None = None,
                     seed: dict | None = None) -> InputSource:
    """优先级:函数内 > 跨函数 > 值关联 > SOURCE_UNKNOWN。"""
    # ① 函数内 provenance(现有)
    from stateforge.feasibility.input_provenance import (
        resolve_operand_source)
    result = resolve_operand_source(app_path, function_name, guard_lineno)
    if result.status == "ok":
        return result

    # ② 跨函数:守卫变量是参数 → 追 caller
    # 从 guard AST 拿变量名
    tree = ast.parse(Path(app_path).read_text(encoding="utf-8"))
    fn = _find_function(tree, function_name)
    if fn:
        param_names = [a.arg for a in fn.args.args]
        test = _test_at(fn, guard_lineno)
        if test and isinstance(test, ast.Compare):
            for side in (test.left, test.comparators[0] if test.comparators else None):
                if isinstance(side, ast.Name) and side.id in param_names:
                    cross = resolve_cross_function(ir, app_path,
                                                   function_name, side.id)
                    if cross:
                        cross.status = "ok"
                        cross.confidence = 0.8     # 跨函数:置信低于直连
                        return cross

    # ③ 动态值关联
    if guard_operands and seed:
        matches = correlate_values(guard_operands, seed)
        if matches:
            best = sorted(matches, key=lambda m: -m.confidence)[0]
            return InputSource(
                best.kind, best.key,
                best.transform, "",
                status="ok", confidence=0.6)    # 值关联:置信最低

    return result       # 全部失败 → 返回原始 SOURCE_UNKNOWN
