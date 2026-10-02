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
             "headers": "HEADER", "cookies": "COOKIE"}
# request.values 是 query+form 组合视图 → AMBIGUOUS(评审 P1-1 冻结);
# headers/cookies 按本轮指令升级为一等源(单一来源,可解析)。
_AMBIGUOUS_ATTRS = {"values", "data", "query_string"}
# 冻结的确定性变换链词汇表(实验前冻结,不增不调)
TRANSFORMS = ("int", "float", "str", "strip", "lower", "upper")


@dataclass
class InputSource:
    kind: str                    # QUERY / FORM / JSON / HEADER / COOKIE
    key: str
    transform: str               # 链摘要:"identity" / "int" / "strip->int"…
    var: str                     # 绑定的变量名
    status: str = "ok"           # ok / SOURCE_UNKNOWN / SOURCE_AMBIGUOUS /
                                 # UNRESOLVED_TRANSFORM / PROVENANCE_UNAVAILABLE
    confidence: float = 1.0
    transforms: tuple = ()       # 变换链(按从源到变量的顺序)


def apply_transforms(raw, chain):
    """按链顺序应用冻结变换(值确认用;不猜逆变换)。"""
    for t in chain:
        if t == "int":
            raw = int(raw)
        elif t == "float":
            raw = float(raw)
        elif t == "str":
            raw = str(raw)
        elif t == "strip":
            raw = raw.strip()
        elif t == "lower":
            raw = raw.lower()
        elif t == "upper":
            raw = raw.upper()
        else:
            raise ValueError(t)
    return raw


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
                and isinstance(expr.slice, ast.Constant):
            if attr.attr in KIND_ATTR:
                return KIND_ATTR[attr.attr], str(expr.slice.value)
            if attr.attr in _AMBIGUOUS_ATTRS:
                return "AMBIGUOUS", attr.attr
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) \
            and expr.func.attr == "get" \
            and isinstance(expr.func.value, ast.Attribute) \
            and isinstance(expr.func.value.value, ast.Name) \
            and expr.func.value.value.id == "request" \
            and expr.func.value.attr in KIND_ATTR \
            and 1 <= len(expr.args) <= 2 and isinstance(expr.args[0], ast.Constant):
        return KIND_ATTR[expr.func.value.attr], str(expr.args[0].value)
    return None


def _mentions_request(value: ast.expr) -> bool:
    """RHS 是否含 request.<kind> 读取(用于区分复杂转换与纯局部计算)。"""
    for node in ast.walk(value):
        if isinstance(node, ast.Attribute) and node.attr in KIND_ATTR:
            if isinstance(node.value, ast.Name) and node.value.id == "request":
                return True
    return False


def _parse_chain(value: ast.expr) -> tuple[list[str], ast.expr]:
    """由外向内剥冻结变换:int(x)/x.strip()/x.lower()/x.upper()…"""
    chain: list[str] = []
    expr = value
    while True:
        if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) \
                and expr.func.id in ("int", "float", "str") and len(expr.args) == 1:
            chain.append(expr.func.id)
            expr = expr.args[0]
        elif isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) \
                and expr.func.attr in ("strip", "lower", "upper") \
                and not expr.args:
            chain.append(expr.func.attr)
            expr = expr.func.value
        else:
            return chain, expr


def _assign_source(stmt: ast.stmt) -> InputSource | None:
    """单条赋值 var = <source expr> / int(<source expr>);不匹配返回 None。"""
    if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
        return None
    targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
    names = [t.id for t in targets if isinstance(t, ast.Name)]
    if not names:
        return None
    chain, base = _parse_chain(stmt.value)
    hit = _extract(base)
    if hit is None or hit[0] == "AMBIGUOUS":
        if _mentions_request(stmt.value):
            status = ("SOURCE_AMBIGUOUS" if (hit and hit[0] == "AMBIGUOUS")
                      else "UNRESOLVED_TRANSFORM")
            return InputSource("", "", "UNRESOLVED", names[0],
                               status=status, confidence=0.0)
        return None                              # 纯局部计算
    kind, key = hit
    transform = "->".join(chain) if chain else "identity"
    return InputSource(kind, key, transform, names[0],
                       transforms=tuple(chain))


def _assign_targets(stmt):
    if isinstance(stmt, ast.Assign):
        return [t.id for t in stmt.targets if isinstance(t, ast.Name)]
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        return [stmt.target.id]
    return []


def _scan_function_before(fn: ast.FunctionDef, guard_line: int
                          ) -> dict[str, InputSource]:
    """guard 词法位置之前的**直线**赋值(按行序,后者覆盖前者)。

    P0-2:guard 之后的赋值不参与(时序正确);分支/循环体内的重定义
    一律 SOURCE_AMBIGUOUS——宁可未知,不给确定但错误的来源。
    """
    direct: list[tuple[int, str, InputSource]] = []
    ambiguous: set[str] = set()

    def visit(stmts, in_branch: bool):
        for s in stmts:
            if isinstance(s, (ast.Assign, ast.AnnAssign)):
                src = _assign_source(s)
                names = _assign_targets(s)
                if src is not None:
                    for n in names:
                        if in_branch:
                            ambiguous.add(n)
                        else:
                            direct.append((s.lineno, n, src))
                elif in_branch and _mentions_request(s.value):
                    ambiguous.update(names)
            elif not isinstance(s, (ast.If, ast.While, ast.For,
                                    ast.AsyncFor, ast.Try)):
                pass
            for field in ("body", "orelse", "finalbody", "handlers"):
                for child in getattr(s, field, []) or []:
                    if isinstance(child, ast.stmt):
                        visit([child], in_branch=True)
                    elif isinstance(child, ast.ExceptHandler):
                        visit(child.body, in_branch=True)

    visit(fn.body, in_branch=False)
    env: dict[str, InputSource] = {}
    for lineno, name, src in sorted(direct, key=lambda t: t[0]):
        if lineno >= guard_line:                   # guard 之后:不参与
            continue
        if name in ambiguous:
            continue
        env[name] = src
    for name in ambiguous:
        if name not in env:
            env[name] = InputSource("", "", "", name,
                                    status="SOURCE_AMBIGUOUS", confidence=0.0)
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
    env = _scan_function_before(fn, guard_lineno)
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
