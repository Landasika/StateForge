"""provenance.py —— 实体变量来源与别名追踪(契约 §6.6.2,关口 A 阶段 2)。

回答"变量从哪里来":对函数内赋值 `X = callee(...)` / `X, err = callee(...)`,
当 callee(同文件可见)体内执行参数化 `SELECT ... FROM <t> [WHERE <k>=?]`
且返回非常量时,建立 X ← 表 t 行的来源链;callee 读 request.headers
(Bearer/Authorization 形态)→ 追认 auth_subject(请求认证主体)。

结构规则(零业务名特判):
1. 局部赋值:单名或元组解包(元组取首元素承承返回行——vulnshop
   `user, err = require_user()` 惯用法);
2. 跨函数:一层内联展开(被调函数体内的 SELECT/request.headers/
   对更内层本地函数的一层透传);
3. dict-store 别名:`X = _DB["t"][k]`(微靶形态)→ 表 t(复用
   extractor._store_table 的容器识别)。

诚实边界:仅解析同文件、单层调用链、常量参数化 SQL;ORM/动态拼接
SQL/外部导入函数 → table=None(调用方记 UnresolvableItem,不猜)。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from stateforge.state.relational import VarProvenance

_SELECT_RE = re.compile(r"^\s*SELECT\b.+?\bFROM\s+(`?\w+`?)",
                        re.IGNORECASE | re.DOTALL)
_WHERE_KEY_RE = re.compile(r"\bWHERE\s+(`?\w+`?)\s*=\s*\?",
                           re.IGNORECASE)
_ROUTE_PARAM_RE = re.compile(r"<(?:int:|string:|path:)?(\w+)>")
_AUTH_HINTS = ("headers", "cookies", "Authorization", "authorization")


@dataclass(frozen=True)
class _CalleeProfile:
    """被调函数的只读剖析:行查询面 + 认证面。"""

    table: str | None
    where_key: str | None
    sql: str
    auth: bool
    returns_nonconst: bool


def _profile(fn: ast.FunctionDef, funcs: dict[str, ast.FunctionDef],
             depth: int = 0) -> _CalleeProfile | None:
    """函数体 → 行查询/认证剖析;无可分析面 → None。"""
    table: str | None = None
    where_key: str | None = None
    sql = ""
    auth = False
    returns_nonconst = False

    for node in ast.walk(fn):
        # request.headers / request.cookies 等(认证面)
        if isinstance(node, ast.Attribute) and node.attr in _AUTH_HINTS:
            auth = True
        # execute("SELECT ... FROM t [WHERE k=?]")(行查询面)
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute" and node.args):
            sql_const = node.args[0]
            if isinstance(sql_const, ast.Constant) \
                    and isinstance(sql_const.value, str):
                m = _SELECT_RE.match(sql_const.value)
                if m:
                    table = m.group(1).strip("`")
                    sql = sql_const.value
                    mk = _WHERE_KEY_RE.search(sql_const.value)
                    if mk:
                        where_key = mk.group(1).strip("`")
        if isinstance(node, ast.Return) and node.value is not None:
            if not isinstance(node.value, ast.Constant):
                returns_nonconst = True

    # 一层透传:callee 自身无 SELECT,但其调用的本地函数有(且唯一定源)
    if table is None and depth < 1:
        inner: _CalleeProfile | None = None
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in funcs
                    and node.func.id != fn.name):
                prof = _profile(funcs[node.func.id], funcs, depth + 1)
                if prof is not None and prof.table is not None:
                    if inner is not None and inner.table != prof.table:
                        return None          # 多表歧义 → 不猜
                    inner = prof
        if inner is not None:
            return _CalleeProfile(table=inner.table, where_key=inner.where_key,
                                  sql=inner.sql, auth=auth or inner.auth,
                                  returns_nonconst=True)
    if table is None and not auth:
        return None
    return _CalleeProfile(table=table, where_key=where_key, sql=sql,
                          auth=auth, returns_nonconst=returns_nonconst)


def _store_table_node(node) -> str | None:
    """`_DB["table"]` 容器常量表名(复用 extractor 的容器识别规则)。"""
    if (isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id in ("_DB", "DB", "db")
            and isinstance(node.slice, ast.Constant)):
        return str(node.slice.value)
    return None


class ProvenanceIndex:
    """(函数名, 局部变量) → VarProvenance 查询 + 变量种类判定。"""

    def __init__(self, funcs: dict[str, ast.FunctionDef],
                 routes: dict[str, set[str]],
                 vars_: dict[tuple[str, str], VarProvenance],
                 assigned: dict[str, set[str]],
                 params: dict[str, set[str]]):
        self._funcs = funcs
        self._routes = routes
        self._vars = vars_
        self._assigned = assigned
        self._params = params

    def lookup(self, fn_name: str, local_var: str) -> VarProvenance | None:
        return self._vars.get((fn_name, local_var))

    def var_kind(self, fn_name: str, name: str) -> str:
        """变量种类:route-param(路由参数)> param > local(函数内赋值)
        > config(闭包/全局,如 guards)——PathCondition.kind 用。"""
        if name in self._routes.get(fn_name, ()):
            return "route-param"
        if name in self._params.get(fn_name, ()):
            return "param"
        if name in self._assigned.get(fn_name, ()):
            return "local"
        return "config"

    def is_local(self, fn_name: str, name: str) -> bool:
        return name in self._assigned.get(fn_name, ())


def analyze_provenance(source_file: str | Path) -> ProvenanceIndex:
    """解析源文件,建立实体变量来源索引(全函数,含嵌套)。"""
    path = Path(source_file)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return ProvenanceIndex({}, {}, {}, {}, {})

    funcs: dict[str, ast.FunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs[node.name] = node          # 同名后见覆盖(嵌套内层优先)

    # 路由参数:装饰器常量路由串中的 <param>
    routes: dict[str, set[str]] = {}
    for name, fn in funcs.items():
        params: set[str] = set()
        for dec in fn.decorator_list:
            targets = [dec] if isinstance(dec, ast.Call) else []
            if isinstance(dec, ast.Call):
                targets = dec.args
            for arg in targets:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    params.update(_ROUTE_PARAM_RE.findall(arg.value))
        if params:
            routes[name] = params

    vars_: dict[tuple[str, str], VarProvenance] = {}
    assigned: dict[str, set[str]] = {}
    params_map: dict[str, set[str]] = {
        n: {a.arg for a in f.args.args} for n, f in funcs.items()}

    for fn_name, fn in funcs.items():
        local_assigned: set[str] = set()
        for node in ast.walk(fn):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                names = ([target.id] if isinstance(target, ast.Name)
                         else [t.id for t in target.elts
                               if isinstance(t, ast.Name)] if isinstance(target, ast.Tuple)
                         else [])
                local_assigned.update(names)
                if not names:
                    continue
                head = names[0]             # 元组解包:首元素承返回行
                origin: tuple[str, ...] = ()
                table = None
                key_param = None
                auth = False
                line = node.lineno
                src = str(path)

                value = node.value
                # 形态 1:X = callee(...) —— 同文件函数调用
                if isinstance(value, ast.Call) \
                        and isinstance(value.func, ast.Name) \
                        and value.func.id in funcs \
                        and value.func.id != fn_name:
                    callee = funcs[value.func.id]
                    prof = _profile(callee, funcs)
                    if prof is not None and prof.returns_nonconst:
                        table = prof.table
                        auth = prof.auth
                        args_repr = ", ".join(ast.unparse(a) for a in value.args)
                        origin = (f"call:{value.func.id}({args_repr})",
                                  *(f"sql:{prof.sql[:60]}" if prof.sql else "",),
                                  *((f"key:{prof.where_key}",)
                                    if prof.where_key else ()))
                        # 行键参数:首个简单名实参(如 oid)
                        if value.args and isinstance(value.args[0], ast.Name):
                            key_param = value.args[0].id
                        elif prof.where_key:
                            key_param = prof.where_key
                # 形态 2:X = _DB["t"][k](dict-store,微靶形态)
                elif (isinstance(value, ast.Subscript)
                      and (t := _store_table_node(value.value)) is not None):
                    table = t
                    origin = (f"store:_DB[{t!r}]",)
                    if isinstance(value.slice, ast.Name):
                        key_param = value.slice.id

                if origin:                   # 有可分析来源才登记
                    vars_[(fn_name, head)] = VarProvenance(
                        var=f"?{head}", table=table, key_param=key_param,
                        origin=origin, auth_subject=auth,
                        source_file=src, source_line=line)
        assigned[fn_name] = local_assigned

    return ProvenanceIndex(funcs, routes, vars_, assigned, params_map)
