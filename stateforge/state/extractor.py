"""Phase-5A:StateFact 静态提取器(AST)。

从目标源码提取 SESSION/DATABASE/FILE 三域的持久状态操作,
判断控制相关性,生成结构化谓词。
"""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path
from typing import TYPE_CHECKING

from stateforge.state.model import (
    StateDomain, StateFactCandidate, StateOperation, Relevance,
    PredicateOp, StructuredPredicate)

if TYPE_CHECKING:  # 仅类型;运行时鸭子式取 ir.blocks,避免硬依赖
    from stateforge.core.ir import ProgramIR

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


# ── CFG 基本块绑定(诊断 3)──

class _BlockIndex:
    """ProgramIR 基本块行索引:(file, lineno) → block_id。

    定向状态构造需要"写入发生在哪个基本块"作为中间目标(fuzz 写入块 =
    构造状态)。匹配规则:同文件 + 行落在 [start_line, end_line];多命中
    取最窄块(嵌套/顺序块重叠时选精确锚);路径形态不一致时按文件名
    兜底。ir 缺省时 lookup 恒 None(行为与未绑定版一致)。
    """

    def __init__(self, ir: "ProgramIR") -> None:
        self._by_file: dict[str, list[tuple[int, int, str]]] = {}
        for b in getattr(ir, "blocks", {}).values():
            self._by_file.setdefault(b.file, []).append(
                (b.start_line, b.end_line, b.id))

    def lookup(self, filename: str, lineno: int) -> str | None:
        spans = self._by_file.get(filename)
        if spans is None:  # 绝对/相对路径形态不一致 → 文件名兜底
            base = filename.replace("\\", "/").rsplit("/", 1)[-1]
            for f, sp in self._by_file.items():
                if f.replace("\\", "/").rsplit("/", 1)[-1] == base:
                    spans = sp
                    break
        if not spans:
            return None
        hits = [(s, e, i) for s, e, i in spans if s <= lineno <= e]
        if not hits:
            return None
        return min(hits, key=lambda t: t[1] - t[0])[2]


# ── 容器 dict 库提取(Phase 1 统一状态读写索引)──
#
# 词汇:`_DB["table"][key] = row` 写 / `_DB["table"][key]`·`.get(key)` 读 /
# `key in _DB["table"]` / `len(_DB["table"])` / `del _DB["table"][key]`;
# 行变量别名(`order = _DB["orders"].get(oid)`)传播后,`order["field"]`
# 读映射为 `db.<table>.<field>`,`order is None` 映射为存在性读。
# object_ref 命名:`db.<table>`(行级)/ `db.<table>.<field>`(字段级)。

_DICT_STORE_NAMES = {"_DB", "DB", "_db", "database"}
_ORM_TXN_METHODS = {
    "commit": StateOperation.WRITE,
    "flush": StateOperation.WRITE,
    "rollback": StateOperation.DELETE,
}


def _is_store_name(name: str) -> bool:
    return name in _DICT_STORE_NAMES


def _store_table(node) -> str | None:
    """`_DB["table"]` → "table"(库容器上的常量表下标)。"""
    if (isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and _is_store_name(node.value.id)
            and isinstance(node.slice, ast.Constant)):
        return str(node.slice.value)
    return None


def _row_expr_table(node) -> str | None:
    """行取值表达式 → 表名:`_DB[t][k]`、`_DB[t].get(k)`(含三元包裹形)。"""
    if isinstance(node, ast.IfExp):               # `... .get(k) if cond else None`
        return _row_expr_table(node.body) or _row_expr_table(node.orelse)
    if (isinstance(node, ast.Subscript) and node.ctx.__class__ is ast.Load
            and _store_table(node.value) is not None):
        return _store_table(node.value)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and _store_table(node.func.value) is not None):
        return _store_table(node.func.value)
    return None


def _row_alias_map(fn_node: ast.AST) -> dict[str, str]:
    """行变量别名:`order = _DB["orders"].get(oid)` → {"order": "orders"}。"""
    alias: dict[str, str] = {}
    for node in ast.walk(fn_node):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            table = _row_expr_table(node.value)
            if table is not None:
                alias[node.targets[0].id] = table
    return alias


def _mk_db_fact(ref: str, op: StateOperation, node, fn, filename,
                predicate: StructuredPredicate | None = None,
                written_value=None) -> StateFactCandidate:
    return StateFactCandidate(
        candidate_id=make_id("DATABASE", ref, op.value, filename, node.lineno),
        domain=StateDomain.DATABASE, object_ref=ref, operation=op,
        source_file=filename, function_name=fn.name,
        lineno=node.lineno, block_id=None,
        predicate=ast.unparse(node)[:80],
        structured_predicate=predicate,
        written_value=written_value,
        relevance=Relevance.NON_CONTROL,
        evidence=(f"{filename}:{node.lineno} dict-store {op.value}",),
    )


def _extract_dict_store(fn_node, filename, in_guard) -> list[StateFactCandidate]:
    facts: list[StateFactCandidate] = []
    alias = _row_alias_map(fn_node)
    assign_targets: set[tuple[str, int]] = set()      # 排除赋值目标读
    for node in ast.walk(fn_node):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                for sub in ast.walk(t):
                    if isinstance(sub, ast.Subscript) and hasattr(sub, "lineno"):
                        assign_targets.add((sub.__class__.__name__
                                            + str(_store_table(sub) or ""),
                                            sub.lineno))

        # 写:_DB["table"][key] = row(目标为二层下标)
        if isinstance(node, ast.Assign):
            for t in node.targets:
                table = _store_table(t.value) if isinstance(t, ast.Subscript) else None
                if table is not None:
                    facts.append(_mk_db_fact(
                        f"db.{table}", StateOperation.WRITE,
                        node, fn_node, filename,
                        written_value=_extract_const(node.value)
                        if isinstance(node.value, (ast.Constant,)) else None))

        # 读(载入语境):_DB["table"][key] 直取
        if (isinstance(node, ast.Subscript) and node.ctx.__class__ is ast.Load
                and (t := _store_table(node.value)) is not None
                and (node.__class__.__name__ + str(t), node.lineno)
                not in assign_targets):
            facts.append(_mk_db_fact(f"db.{t}", StateOperation.READ,
                                     node, fn_node, filename))

        # 读:_DB["table"].get(key) / len(_DB["table"])
        if isinstance(node, ast.Call):
            if (isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and (t := _store_table(node.func.value)) is not None):
                facts.append(_mk_db_fact(f"db.{t}", StateOperation.READ,
                                         node, fn_node, filename))
            elif (isinstance(node.func, ast.Name) and node.func.id == "len"
                    and node.args and (t := _store_table(node.args[0])) is not None):
                facts.append(_mk_db_fact(f"db.{t}", StateOperation.READ,
                                         node, fn_node, filename))

        # 读:key in _DB["table"](成员检查)
        if (isinstance(node, ast.Compare)
                and any(isinstance(op, ast.In) for op in node.ops)):
            for side in (node.left, *node.comparators):
                if (t := _store_table(side)) is not None:
                    facts.append(_mk_db_fact(
                        f"db.{t}", StateOperation.READ, node, fn_node,
                        filename,
                        predicate=StructuredPredicate(PredicateOp.EXISTS,
                                                      f"db.{t}")))

        # 删:del _DB["table"][key]
        if isinstance(node, ast.Delete):
            for t_target in node.targets:
                if (isinstance(t_target, ast.Subscript)
                        and (t := _store_table(t_target.value)) is not None):
                    facts.append(_mk_db_fact(f"db.{t}", StateOperation.DELETE,
                                             node, fn_node, filename))

        # ORM 事务点:db.session.commit/flush/rollback
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _ORM_TXN_METHODS):
            facts.append(_mk_db_fact(
                f"db.session.{node.func.attr}",
                _ORM_TXN_METHODS[node.func.attr], node, fn_node, filename))

    # 行变量字段读 + 存在性(None 比较)+ 比较谓词(EQ/NE/IN)
    _cmp_op_map = {"Eq": PredicateOp.EQ, "NotEq": PredicateOp.NE,
                   "In": PredicateOp.IN, "NotIn": PredicateOp.NOT_IN}
    compare_of: dict[int, tuple[PredicateOp, object]] = {}
    for node in ast.walk(fn_node):
        if (isinstance(node, ast.Compare) and len(node.ops) == 1
                and type(node.ops[0]).__name__ in _cmp_op_map):
            for i, side in enumerate((node.left, *node.comparators)):
                other = node.comparators[0] if i == 0 else node.left
                if isinstance(side, ast.Subscript):
                    compare_of[id(side)] = (
                        _cmp_op_map[type(node.ops[0]).__name__],
                        _extract_const(other))
    for node in ast.walk(fn_node):
        if (isinstance(node, ast.Subscript) and node.ctx.__class__ is ast.Load
                and isinstance(node.value, ast.Name)
                and node.value.id in alias
                and isinstance(node.slice, ast.Constant)):
            var, field = node.value.id, str(node.slice.value)
            pred = None
            if id(node) in compare_of:
                op, val = compare_of[id(node)]
                pred = StructuredPredicate(op, f"db.{alias[var]}.{field}", val)
            facts.append(_mk_db_fact(
                f"db.{alias[var]}.{field}", StateOperation.READ,
                node, fn_node, filename, predicate=pred))
        if (isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Name)
                and node.left.id in alias
                and any(isinstance(c, ast.Constant) and c.value is None
                        for c in node.comparators)):
            facts.append(_mk_db_fact(
                f"db.{alias[node.left.id]}", StateOperation.READ,
                node, fn_node, filename,
                predicate=StructuredPredicate(PredicateOp.EXISTS,
                                              f"db.{alias[node.left.id]}")))
    return facts


# ── 主入口 ──

def extract_state_facts(source_dir: str | Path,
                        ir: "ProgramIR | None" = None) -> list[StateFactCandidate]:
    """从目录提取全部 StateFact(SESSION + DATABASE + FILE)。

    ir 给定时(诊断 3),每条事实经行号锚到 ICFG 基本块(block_id);
    前提:ir 与 source_dir 同源(同一目标树的 build_icfg 产物)。
    """
    source_dir = Path(source_dir)
    block_index = _BlockIndex(ir) if ir is not None else None
    all_facts = []
    for py in sorted(source_dir.rglob("*.py")):
        if "__pycache__" in str(py) or py.name.startswith("."):
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"),
                              filename=str(py))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            in_guard = False   # 简化:整体函数标记(增强版可逐行)
            facts = []
            facts.extend(_extract_session(fn, str(py), in_guard))
            facts.extend(_extract_db(fn, str(py), in_guard))
            facts.extend(_extract_file(fn, str(py), in_guard))
            facts.extend(_extract_dict_store(fn, str(py), in_guard))

            # 重新判定控制相关性:在 if 条件中使用的操作 → CONTROL
            # (诊断 3:同时锚 block_id——写入块 = 定向状态构造的中间目标)
            import dataclasses
            for fact in facts:
                updates: dict = {}
                if fact.lineno and _is_in_guard(fn, fact.lineno):
                    updates["relevance"] = Relevance.CONTROL
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
                if block_index is not None and fact.lineno:
                    bid = block_index.lookup(fact.source_file, fact.lineno)
                    if bid is not None:
                        updates["block_id"] = bid
                all_facts.append(
                    dataclasses.replace(fact, **updates) if updates else fact)

    # 去重(同 ID)
    seen = {}
    for f in all_facts:
        seen[f.candidate_id] = f
    return sorted(seen.values(), key=lambda f: f.candidate_id)
