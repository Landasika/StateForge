"""Phase-4.1B Level 4:Input Provenance(最小静态数据流,非 taint)。

只回答:guard 的输入侧操作数,来自当前请求的 QUERY/FORM/JSON 哪个字段。
支持:直接读取(request.args["x"] / .get)与 int() 一层转换;
其余一律拒绝(UNRESOLVED_TRANSFORM / SOURCE_UNKNOWN),禁止猜。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

KIND_ATTR = {"args": "QUERY", "form": "FORM", "json": "JSON",
             "values": "FORM"}


@dataclass
class InputSource:
    kind: str                    # QUERY / FORM / JSON
    key: str
    transform: str               # identity / int
    var: str                     # 绑定的变量名
    status: str = "ok"           # ok / SOURCE_UNKNOWN / UNRESOLVED_TRANSFORM
    confidence: float = 1.0


def _match_source(expr: ast.expr) -> tuple[str, str] | None:
    """request.<attr>["key"] / request.<attr>.get("key") → (kind, key)。"""
    inner = expr
    if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute) \
            and inner.func.attr == "get" and len(inner.args) == 1 \
            and isinstance(inner.args[0], ast.Constant):
        inner = inner.func.value
        key = None
        # get 形式的 key 在 args;重取
        return None  # 占位,下面统一处理
    return None


def _extract(expr: ast.expr) -> tuple[str, str] | None:
    """统一提取:Subscript(request.<attr>[Constant]) 或 .get(Constant)。"""
    if isinstance(expr, ast.Subscript) and isinstance(expr.value, ast.Attribute):
        attr = expr.value
        if isinstance(attr.value, ast.Name) and attr.value.id == "request" \
                and attr.attr in KIND_ATTR \
                and isinstance(expr.slice, ast.Constant):
            return KIND_ATTR[attr.attr], str(expr.slice.value)
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) \
            and expr.func.attr == "get" \
            and isinstance(expr.func.value, ast.Attribute) \
            and isinstance(expr.func.value.value, ast.Name) \
            and expr.func.value.value.id == "request" \
            and expr.func.value.attr in KIND_ATTR \
            and len(expr.args) == 1 and isinstance(expr.args[0], ast.Constant):
        return KIND_ATTR[expr.func.value.attr], str(expr.args[0].value)
    return None


def _mentions_request(value: ast.expr) -> bool:
    """RHS 是否含 request.<kind> 读取(用于区分复杂转换与纯局部计算)。"""
    for node in ast.walk(value):
        if isinstance(node, ast.Attribute) and node.attr in KIND_ATTR:
            if isinstance(node.value, ast.Name) and node.value.id == "request":
                return True
    return False


def _assign_source(stmt: ast.stmt) -> InputSource | None:
    """单条赋值 var = <source expr> / int(<source expr>);不匹配返回 None。"""
    if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
        return None
    targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
    names = [t.id for t in targets if isinstance(t, ast.Name)]
    if not names:
        return None
    value = stmt.value
    transform = "identity"
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
        if value.func.id == "int" and len(value.args) == 1:
            inner = value.args[0]
            hit = _extract(inner)
            if hit is None:
                if _mentions_request(inner):
                    return InputSource("", "", "UNRESOLVED", names[0],
                                       status="UNRESOLVED_TRANSFORM")
                return None
            kind, key = hit
            return InputSource(kind, key, "int", names[0])
        if _mentions_request(value):             # int 以外的函数包裹 → 拒绝
            return InputSource("", "", "UNRESOLVED", names[0],
                               status="UNRESOLVED_TRANSFORM", confidence=0.0)
        return None                              # 纯局部函数调用
    hit = _extract(value)
    if hit is None:
        if _mentions_request(value):             # 复杂表达式提及 request → 拒绝
            return InputSource("", "", "UNRESOLVED", names[0],
                               status="UNRESOLVED_TRANSFORM", confidence=0.0)
        return None                              # 非输入赋值(局部计算)
    kind, key = hit
    return InputSource(kind, key, transform, names[0])


def _scan_function(fn: ast.FunctionDef) -> dict[str, InputSource]:
    """函数内直线扫描赋值(顺序无关:同名重写覆盖;不支持分支敏感)。"""
    env: dict[str, InputSource] = {}
    for stmt in ast.walk(fn):
        src = _assign_source(stmt)
        if src is not None:
            env[src.var] = src
    return env


def resolve_operand_source(app_path: str | Path, function: str,
                           guard_lineno: int) -> InputSource:
    """解析 guard 行上的输入侧来源;按 guard 的 Compare 结构选 Name 侧。"""
    tree = ast.parse(Path(app_path).read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == function), None)
    if fn is None:
        return InputSource("", "", "", "", status="SOURCE_UNKNOWN", confidence=0.0)
    env = _scan_function(fn)
    test = _test_at(fn, guard_lineno)
    if test is None or not isinstance(test, ast.Compare) \
            or len(test.ops) != 1:
        return InputSource("", "", "", "", status="SOURCE_UNKNOWN", confidence=0.0)
    for side in (test.left, test.comparators[0]):
        if isinstance(side, ast.Name):
            hit = env.get(side.id)
            if hit is not None:
                return hit
            # 变量存在但来源是复杂转换 → 早已在 env 里标 UNRESOLVED_TRANSFORM
    return InputSource("", "", "", "", status="SOURCE_UNKNOWN", confidence=0.0)


def _test_at(fn: ast.FunctionDef, lineno: int) -> ast.expr | None:
    for stmt in ast.walk(fn):
        if isinstance(stmt, (ast.If, ast.While)) \
                and getattr(stmt, "test", None) is not None \
                and stmt.test.lineno == lineno:
            return stmt.test
    return None
