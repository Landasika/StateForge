"""Phase-5A:StateFact 静态提取器(AST)。

从目标源码提取 SESSION/DATABASE/FILE 三域的持久状态操作,
判断控制相关性,生成结构化谓词。
"""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path

from stateforge.state.model import (
    StateDomain, StateFactCandidate, StateOperation, Relevance,
    PredicateOp, StructuredPredicate)

# ── Canonical object_ref ──

def canon_session(key: str) -> str:
    return f"session.{key}"

def canon_db(entity: str, attr: str) -> str:
    return f"{entity}.{attr}"

def canon_file(path: str) -> str:
    return f"FILE:{path}"


# ── 确定性 candidate_id ──

def make_id(domain: str, obj_ref: str, op: str, file: str, line: int) -> str:
    raw = f"{domain}:{obj_ref}:{op}:{file}:{line}"
    h = hashlib.sha256(raw.encode()).hexdigest()[:8]
    return f"SF-{h}"


# ── Session 提取 ──

_SESSION_METHODS = {"get": StateOperation.READ, "pop": StateOperation.DELETE,
                    "clear": StateOperation.DELETE}


def _extract_session(fn_node: ast.FunctionDef, filename: str,
                     in_guard: bool) -> list[StateFactCandidate]:
    facts = []
    for node in ast.walk(fn_node):
        # session["key"] = value  (WRITE)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "session"
                        and isinstance(target.slice, ast.Constant)):
                    key = str(target.slice.value)
                    val = _extract_const(node.value)
                    facts.append(StateFactCandidate(
                        candidate_id=make_id("SESSION", canon_session(key),
                                             "WRITE", filename, node.lineno),
                        domain=StateDomain.SESSION,
                        object_ref=canon_session(key),
                        operation=StateOperation.WRITE,
                        source_file=filename, function_name=fn_node.name,
                        lineno=node.lineno, block_id=None,
                        predicate=ast.unparse(node),
                        structured_predicate=None,
                        written_value=val,
                        relevance=Relevance.NON_CONTROL,
                        evidence=(f"{filename}:{node.lineno} write",),
                    ))

        # session.get("key") / session.pop("key") / "key" in session
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            obj = node.func.value
            method = node.func.attr
            if isinstance(obj, ast.Name) and obj.id == "session" \
                    and method in _SESSION_METHODS:
                op = _SESSION_METHODS[method]
                if node.args and isinstance(node.args[0], ast.Constant):
                    key = str(node.args[0].value)
                    facts.append(StateFactCandidate(
                        candidate_id=make_id("SESSION", canon_session(key),
                                             op.value, filename, node.lineno),
                        domain=StateDomain.SESSION,
                        object_ref=canon_session(key), operation=op,
                        source_file=filename, function_name=fn_node.name,
                        lineno=node.lineno, block_id=None,
                        predicate=ast.unparse(node),
                        structured_predicate=_session_predicate(
                            canon_session(key), node, in_guard),
                        written_value=None,
                        relevance=(Relevance.CONTROL if in_guard
                                   else Relevance.NON_CONTROL),
                        evidence=(f"{filename}:{node.lineno} "
                                  f"session.{method}",),
                    ))

        # session["key"] 读(非赋值,如 if session["user_id"] 或 y = session["key"])
        if isinstance(node, ast.Subscript) \
                and isinstance(node.value, ast.Name) \
                and node.value.id == "session" \
                and isinstance(node.slice, ast.Constant):
            # 排除赋值目标(上面已处理)
            is_target = False
            for assign in ast.walk(fn_node):
                if isinstance(assign, ast.Assign):
                    for t in assign.targets:
                        if isinstance(t, ast.Subscript) \
                                and t.lineno == node.lineno:
                            is_target = True
            if not is_target:
                key = str(node.slice.value)
                facts.append(StateFactCandidate(
                    candidate_id=make_id("SESSION", canon_session(key),
                                         "READ", filename, node.lineno),
                    domain=StateDomain.SESSION,
                    object_ref=canon_session(key),
                    operation=StateOperation.READ,
                    source_file=filename, function_name=fn_node.name,
                    lineno=node.lineno, block_id=None,
                    predicate=ast.unparse(node),
                    structured_predicate=_session_predicate(
                        canon_session(key), node, in_guard),
                    written_value=None,
                    relevance=(Relevance.CONTROL if in_guard
                               else Relevance.NON_CONTROL),
                    evidence=(f"{filename}:{node.lineno} session read",),
                ))

        # del session["key"]
        if isinstance(node, ast.Delete):
            for target in node.targets:
                if isinstance(target, ast.Subscript) \
                        and isinstance(target.value, ast.Name) \
                        and target.value.id == "session" \
                        and isinstance(target.slice, ast.Constant):
                    key = str(target.slice.value)
                    facts.append(StateFactCandidate(
                        candidate_id=make_id("SESSION", canon_session(key),
                                             "DELETE", filename, node.lineno),
                        domain=StateDomain.SESSION,
                        object_ref=canon_session(key),
                        operation=StateOperation.DELETE,
                        source_file=filename, function_name=fn_node.name,
                        lineno=node.lineno, block_id=None,
                        predicate=ast.unparse(node),
                        structured_predicate=None, written_value=None,
                        relevance=Relevance.NON_CONTROL,
                        evidence=(f"{filename}:{node.lineno} session del",),
                    ))
    return facts


def _session_predicate(obj_ref: str, node: ast.AST,
                       in_guard: bool) -> StructuredPredicate | None:
    if not in_guard:
        return None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "get":
            return StructuredPredicate(PredicateOp.EXISTS, obj_ref)
    if isinstance(node, ast.Subscript):
        return StructuredPredicate(PredicateOp.TRUTHY, obj_ref)
    return None


# ── Database 提取 ──

def _extract_db(fn_node: ast.FunctionDef, filename: str,
                in_guard: bool) -> list[StateFactCandidate]:
    facts = []
    for node in ast.walk(fn_node):
        # obj.attr = value (如 order.status = "PAID")
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) \
                        and isinstance(target.value, ast.Name):
                    obj = _guess_entity(target.value.id)
                    attr = target.attr
                    ref = canon_db(obj, attr)
                    val = _extract_const(node.value)
                    facts.append(StateFactCandidate(
                        candidate_id=make_id("DATABASE", ref, "WRITE",
                                             filename, node.lineno),
                        domain=StateDomain.DATABASE, object_ref=ref,
                        operation=StateOperation.WRITE,
                        source_file=filename, function_name=fn_node.name,
                        lineno=node.lineno, block_id=None,
                        predicate=ast.unparse(node),
                        structured_predicate=None,
                        written_value=val,
                        relevance=Relevance.NON_CONTROL,
                        evidence=(f"{filename}:{node.lineno} attr write",),
                    ))

        # db().execute("UPDATE ... SET col=? ...") 或 .execute(sql)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "execute" and node.args:
            sql_arg = node.args[0]
            sql = _extract_const(sql_arg)
            if sql and isinstance(sql, str):
                facts.extend(_parse_sql(sql, node, fn_node, filename))

        # if obj.attr == value (守卫中的 DB 读)
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare):
            test = node.test
            if isinstance(test.left, ast.Attribute) \
                    and isinstance(test.left.value, ast.Name) \
                    and len(test.ops) == 1 and test.comparators:
                obj = _guess_entity(test.left.value.id)
                attr = test.left.attr
                ref = canon_db(obj, attr)
                val = _extract_const(test.comparators[0])
                op = type(test.ops[0]).__name__
                pred_op = {"Eq": PredicateOp.EQ, "NotEq": PredicateOp.NE,
                           "In": PredicateOp.IN}.get(op)
                if pred_op:
                    facts.append(StateFactCandidate(
                        candidate_id=make_id("DATABASE", ref, "READ",
                                             filename, node.lineno),
                        domain=StateDomain.DATABASE, object_ref=ref,
                        operation=StateOperation.READ,
                        source_file=filename, function_name=fn_node.name,
                        lineno=node.lineno, block_id=None,
                        predicate=ast.unparse(test),
                        structured_predicate=StructuredPredicate(
                            pred_op, ref, val),
                        written_value=None,
                        relevance=Relevance.CONTROL,
                        evidence=(f"{filename}:{node.lineno} db guard",),
                    ))
    return facts


_SQL_UPDATE = re.compile(
    r"UPDATE\s+(\w+)\s+SET\s+(\w+)", re.IGNORECASE)
_SQL_SELECT = re.compile(
    r"SELECT\s+.*?\s+FROM\s+(\w+)", re.IGNORECASE)


def _parse_sql(sql: str, node: ast.Call, fn: ast.FunctionDef,
               filename: str) -> list[StateFactCandidate]:
    facts = []
    m = _SQL_UPDATE.search(sql)
    if m:
        table, col = m.group(1), m.group(2)
        ref = canon_db(table.capitalize(), col)
        facts.append(StateFactCandidate(
            candidate_id=make_id("DATABASE", ref, "WRITE",
                                 filename, node.lineno),
            domain=StateDomain.DATABASE, object_ref=ref,
            operation=StateOperation.WRITE,
            source_file=filename, function_name=fn.name,
            lineno=node.lineno, block_id=None,
            predicate=sql[:80],
            structured_predicate=None, written_value="UNKNOWN",
            relevance=Relevance.NON_CONTROL,
            evidence=(f"{filename}:{node.lineno} SQL UPDATE",),
        ))
    m2 = _SQL_SELECT.search(sql)
    if m2:
        table = m2.group(1)
        ref = canon_db(table.capitalize(), "*")
        facts.append(StateFactCandidate(
            candidate_id=make_id("DATABASE", ref, "READ",
                                 filename, node.lineno),
            domain=StateDomain.DATABASE, object_ref=ref,
            operation=StateOperation.READ,
            source_file=filename, function_name=fn.name,
            lineno=node.lineno, block_id=None,
            predicate=sql[:80],
            structured_predicate=None, written_value=None,
            relevance=Relevance.NON_CONTROL,
            evidence=(f"{filename}:{node.lineno} SQL SELECT",),
        ))
    return facts


def _guess_entity(var_name: str) -> str:
    """snake_case → PascalCase(order → Order)。"""
    return "".join(w.capitalize() for w in var_name.split("_"))


# ── File 提取 ──

_FILE_READ_METHODS = {"exists", "read_text", "read_bytes", "read"}
_FILE_WRITE_METHODS = {"write_text", "write_bytes", "write"}
_FILE_DELETE_METHODS = {"unlink", "remove"}


def _extract_file(fn_node: ast.FunctionDef, filename: str,
                  in_guard: bool) -> list[StateFactCandidate]:
    facts = []
    for node in ast.walk(fn_node):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            method = node.func.attr
            obj = node.func.value

            # Path("...").method()
            path = None
            if isinstance(obj, ast.Call) and isinstance(obj.func, ast.Name) \
                    and obj.func.id == "Path" and obj.args:
                path = _extract_const(obj.args[0])
            # os.path.exists("...") / os.remove("...")
            elif isinstance(obj, ast.Attribute) \
                    and isinstance(obj.value, ast.Name) and obj.value.id == "os":
                path = _extract_const(node.args[0]) if node.args else None

            if path is None and node.args:
                path = _extract_const(node.args[0])
            if path is None:
                path = "<dynamic>"

            if method in _FILE_READ_METHODS:
                ref = canon_file(str(path))
                facts.append(StateFactCandidate(
                    candidate_id=make_id("FILE", ref, "READ",
                                         filename, node.lineno),
                    domain=StateDomain.FILE, object_ref=ref,
                    operation=StateOperation.READ,
                    source_file=filename, function_name=fn_node.name,
                    lineno=node.lineno, block_id=None,
                    predicate=ast.unparse(node),
                    structured_predicate=StructuredPredicate(
                        PredicateOp.EXISTS, ref) if in_guard else None,
                    written_value=None,
                    relevance=(Relevance.CONTROL if in_guard
                               else Relevance.NON_CONTROL),
                    evidence=(f"{filename}:{node.lineno} file read",),
                ))
            elif method in _FILE_WRITE_METHODS:
                ref = canon_file(str(path))
                facts.append(StateFactCandidate(
                    candidate_id=make_id("FILE", ref, "WRITE",
                                         filename, node.lineno),
                    domain=StateDomain.FILE, object_ref=ref,
                    operation=StateOperation.WRITE,
                    source_file=filename, function_name=fn_node.name,
                    lineno=node.lineno, block_id=None,
                    predicate=ast.unparse(node),
                    structured_predicate=None,
                    written_value=None,
                    relevance=Relevance.NON_CONTROL,
                    evidence=(f"{filename}:{node.lineno} file write",),
                ))
            elif method in _FILE_DELETE_METHODS:
                ref = canon_file(str(path))
                facts.append(StateFactCandidate(
                    candidate_id=make_id("FILE", ref, "DELETE",
                                         filename, node.lineno),
                    domain=StateDomain.FILE, object_ref=ref,
                    operation=StateOperation.DELETE,
                    source_file=filename, function_name=fn_node.name,
                    lineno=node.lineno, block_id=None,
                    predicate=ast.unparse(node),
                    structured_predicate=None,
                    written_value=None,
                    relevance=Relevance.NON_CONTROL,
                    evidence=(f"{filename}:{node.lineno} file delete",),
                ))

        # open("...") → READ(除非 mode='w')
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "open" and node.args:
            path = _extract_const(node.args[0]) or "<dynamic>"
            mode = _extract_const(node.args[1]) if len(node.args) > 1 else "r"
            op = StateOperation.WRITE if mode and "w" in str(mode) \
                else StateOperation.READ
            ref = canon_file(str(path))
            facts.append(StateFactCandidate(
                candidate_id=make_id("FILE", ref, op.value,
                                     filename, node.lineno),
                domain=StateDomain.FILE, object_ref=ref, operation=op,
                source_file=filename, function_name=fn_node.name,
                lineno=node.lineno, block_id=None,
                predicate=ast.unparse(node),
                structured_predicate=None,
                written_value=None,
                relevance=Relevance.NON_CONTROL,
                evidence=(f"{filename}:{node.lineno} open",),
            ))
    return facts


# ── 共用 ──

def _extract_const(node: ast.AST):
    if isinstance(node, ast.Constant):
        return node.value
    return None


def _is_in_guard(fn_node: ast.FunctionDef, lineno: int) -> bool:
    """判断某行是否在 if/while 的 test 表达式内(直接控制依赖)。"""
    for node in ast.walk(fn_node):
        if isinstance(node, (ast.If, ast.While)):
            test = getattr(node, "test", None)
            if test and hasattr(test, "lineno"):
                if test.lineno <= lineno <= getattr(test, "end_lineno",
                                                    test.lineno):
                    return True
    return False


# ── 主入口 ──

def extract_state_facts(source_dir: str | Path) -> list[StateFactCandidate]:
    """从目录提取全部 StateFact(SESSION + DATABASE + FILE)。"""
    source_dir = Path(source_dir)
    all_facts = []
    for py in sorted(source_dir.rglob("*.py")):
        if "__pycache__" in str(py) or py.name.startswith("."):
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"),
                              filename=str(py))
        except (OSError, SyntaxError):
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            in_guard = False   # 简化:整体函数标记(增强版可逐行)
            facts = []
            facts.extend(_extract_session(fn, str(py), in_guard))
            facts.extend(_extract_db(fn, str(py), in_guard))
            facts.extend(_extract_file(fn, str(py), in_guard))

            # 重新判定控制相关性:在 if 条件中使用的操作 → CONTROL
            import dataclasses
            for fact in facts:
                if fact.lineno and _is_in_guard(fn, fact.lineno):
                    updates = {"relevance": Relevance.CONTROL}
                    if fact.structured_predicate is None:
                        # 在守卫中 → 补谓词
                        if fact.domain == StateDomain.SESSION:
                            updates["structured_predicate"] = (
                                StructuredPredicate(
                                    PredicateOp.EXISTS, fact.object_ref))
                        elif fact.domain == StateDomain.DATABASE:
                            updates["structured_predicate"] = (
                                StructuredPredicate(
                                    PredicateOp.TRUTHY, fact.object_ref))
                        elif fact.domain == StateDomain.FILE:
                            updates["structured_predicate"] = (
                                StructuredPredicate(
                                    PredicateOp.EXISTS, fact.object_ref))
                    all_facts.append(dataclasses.replace(fact, **updates))
                else:
                    all_facts.append(fact)

    # 去重(同 ID)
    seen = {}
    for f in all_facts:
        seen[f.candidate_id] = f
    return sorted(seen.values(), key=lambda f: f.candidate_id)
