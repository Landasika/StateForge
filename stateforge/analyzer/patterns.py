"""patterns.py —— SF1-T3 AST 模式规则:代码事实 → StateCandidate + 四类记录。

职责边界(ADR-0009):**patterns 不做安全裁决**——只产候选与结构化事实,
安全语义由标注层(规则|LLM)负责。

覆盖模式(vulnshop/Flask + sqlite3 系;VAmPI/SQLAlchemy 系见 idioms 分派):
  R1  路由装饰器        @app.get/post/...("/x") @app.route(...,methods=)
  R2  SQL 常量写         execute("UPDATE t SET c='v' WHERE ...") / INSERT INTO
  R3  行下标比较         row["attr"] ==/!= "LIT" 、if row["attr"]: / if not
  R4  守卫(旗标分支内)   owner 比对 / role 比对 / 状态前置 → Guard
  R5  token 产/需        make_token(...) → Session.authed_as 产;current_user/校验 → 需
  R6  响应构造           jsonify({...}) 键 → observes
  R7  全局旗标           X = int(os.getenv("X", "0")) → config_flags
  R8  请求输入           body=request.get_json() 后 body.get("k") → Param
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict

from stateforge.core.model import (
    CandidateSource,
    Effect,
    Endpoint,
    Guard,
    HttpMethod,
    Param,
    Precondition,
    Sink,
    StateCandidate,
)

# ---------------------------------------------------------------- 工具


def _lit(node) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _subscript_parts(node) -> tuple[str, str] | None:
    """row["attr"] → (row, "attr");仅常量下标。"""
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        base = node.value
        if isinstance(base, ast.Name):
            return base.id, str(node.slice.value)
        if isinstance(base, ast.Call):  # db().execute(...) 形态少见,忽略
            return None
    return None


_SINGULAR = {"orders": "Order", "coupons": "Coupon", "users": "User", "books": "Book"}


def entity_of_table(table: str) -> str:
    if table in _SINGULAR:
        return _SINGULAR[table]
    if table.endswith("s"):
        return table[:-1].capitalize()
    return table.capitalize()


_UPDATE_RE = re.compile(r"UPDATE\s+(\w+)\s+SET\s+(.+?)\s+WHERE", re.IGNORECASE | re.DOTALL)
_INSERT_RE = re.compile(
    r"INSERT\s+INTO\s+(\w+)\s*\(([^)]*)\)\s*VALUES\s*\(([^)]*)\)", re.IGNORECASE
)


def parse_sql_effects(sql: str) -> list[tuple[str, str, str | None]]:
    """常量 SQL → [(var, 'set', value)]。参数化值(?)不产 Effect(值未知)。"""
    out: list[tuple[str, str, str | None]] = []
    m = _UPDATE_RE.search(sql)
    if m:
        table, sets = m.group(1), m.group(2)
        for piece in sets.split(","):
            if "=" not in piece:
                continue
            col, val = piece.split("=", 1)
            col, val = col.strip(), val.strip().strip("'\"")
            if val == "?":
                continue
            out.append((f"{entity_of_table(table)}.{col}", "set", val))
        return out
    m = _INSERT_RE.search(sql)
    if m:
        table, cols, vals = m.group(1), m.group(2), m.group(3)
        cols = [c.strip() for c in cols.split(",")]
        vals = [v.strip() for v in vals.split(",")]
        for col, val in zip(cols, vals):
            if col in ("id",):
                continue
            if val.startswith(("'", '"')):
                out.append((f"{entity_of_table(table)}.{col}", "set", val.strip("'\"")))
            elif re.fullmatch(r"\d+", val):
                out.append((f"{entity_of_table(table)}.{col}", "set", val))
        return out
    return out


# ---------------------------------------------------------------- 端点提取


class EndpointExtractor(ast.NodeVisitor):
    """对单文件做路由级提取;结果汇集到 self.endpoints / self.candidates。"""

    def __init__(self, file: str, config_flags: dict[str, int] | None = None):
        self.file = file
        self.endpoints: list[Endpoint] = []
        self.candidates: list[StateCandidate] = []
        self.config_flags: dict[str, int] = config_flags if config_flags is not None else {}

    # ---- 顶层:模块内所有 FunctionDef(含工厂函数内的嵌套路由)
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for route, methods in _route_of(node):
            self._extract_endpoint(node, route, methods)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    # ---- 模块级旗标 R7
    def visit_Assign(self, node: ast.Assign) -> None:
        if (
            len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
            and _is_os_getenv_int(node.value)
        ):
            name = node.targets[0].id
            self.config_flags[name.upper()] = _getenv_default(node.value)
        self.generic_visit(node)

    # ---- 端点体提取
    def _extract_endpoint(self, func: ast.FunctionDef, route: str, methods: list[HttpMethod]) -> None:
        ep = Endpoint(
            func=func.name, route=route, methods=methods,
            file=self.file, line=func.lineno,
            params=_path_params(route),
        )
        self._extract_endpoint_into(func, route, methods, ep)
        self.endpoints.append(ep)

    def _extract_endpoint_into(
        self, func: ast.FunctionDef, route: str, methods: list[HttpMethod], ep: Endpoint
    ) -> _EpEnv:
        env = _EpEnv(self.file, ep, self.candidates, self.config_flags)
        for stmt in ast.walk(func):
            env.visit_node(stmt)
        # 端点内事实去重(vuln/fixed 双分支常产生同形 Effect/Precondition)
        ep.effects = _dedup_by(ep.effects, key=lambda e: (e.var, e.op, e.value, e.key))
        ep.preconditions = _dedup_by(
            ep.preconditions, key=lambda p: (p.var, p.op, p.value, p.key)
        )
        return env


class _EpEnv:
    """单端点内的事实收集器。"""

    def __init__(self, file, ep: Endpoint, candidates: list[StateCandidate], flags: dict):
        self.file, self.ep, self.cands, self.flags = file, ep, candidates, flags
        self.json_bodies: set[str] = set()  # request.get_json() 赋值的变量名
        self._cand_ops: dict[str, set] = defaultdict(set)
        self._cand_evidence: dict[str, list] = defaultdict(list)

    def visit_node(self, node) -> None:
        if isinstance(node, ast.Call):
            self._call(node)
        elif isinstance(node, ast.Compare):
            self._compare(node)
        elif isinstance(node, ast.If):
            self._if(node)
        elif isinstance(node, ast.Assign):
            self._assign(node)
        elif isinstance(node, ast.Return):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    self._jsonify(sub)

    # ---- R2 SQL / R5 token / R6 jsonify / R8 body.get / sink
    _AUTHN_PRODUCE = {"make_token", "encode_auth_token", "jwt_encode"}
    _AUTHN_REQUIRE = {"current_user", "token_validator", "decode_auth_token", "require_user"}

    def _call(self, call: ast.Call) -> None:
        name = _call_name(call)
        bare = _bare_name(call)
        if name.endswith(".execute"):
            sql = _lit(call.args[0]) if call.args else None
            if sql:
                for var, op, val in parse_sql_effects(sql):
                    self.ep.effects.append(Effect(
                        var=var, op=op, value=val, file=self.file,
                        line=_line(call, sql),
                    ))
                    self._cand(var, "write", f"execute({sql[:60]}...)", "db_field")
            elif call.args and _is_concat(call.args[0]):
                self.ep.sinks.append(Sink(
                    kind="sql_build", file=self.file, line=getattr(call, "lineno", 0),
                    snippet=ast.dump(call.args[0])[:120],
                ))
        elif bare in self._AUTHN_PRODUCE or _any_name(call) in self._AUTHN_PRODUCE:
            self.ep.effects.append(Effect(
                var="Session.authed_as", op="set", key="username",
                file=self.file, line=getattr(call, "lineno", 0),
            ))
            self._cand("Session.authed_as", "write", bare, "session_access")
        elif bare in self._AUTHN_REQUIRE or _any_name(call) in self._AUTHN_REQUIRE:
            self.ep.preconditions.append(Precondition(
                var="Session.authed_as", op="truthy", file=self.file,
                line=getattr(call, "lineno", 0),
            ))
            self._cand("Session.authed_as", "read", bare, "session_access")
        elif bare == "jsonify":
            self._jsonify(call)
        elif name.endswith(".get") and isinstance(call.func, ast.Attribute) and (
            (isinstance(call.func.value, ast.Name) and call.func.value.id in self.json_bodies)
            or _is_inline_json(call.func.value)
        ):
            if call.args and isinstance(call.args[0], ast.Constant):
                self.ep.params.append(Param(name=str(call.args[0].value), location="json"))

    def _jsonify(self, call: ast.Call) -> None:
        if _any_name(call) != "jsonify" or not call.args:
            return
        arg = call.args[0]
        keys: list[str] = []
        if isinstance(arg, ast.Dict):
            keys = [k.value for k in arg.keys if isinstance(k, ast.Constant)]
        elif isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            try:
                import json
                keys = list(json.loads(arg.value).keys())
            except Exception:
                pass
        self.ep.observes.extend(k for k in keys if k not in self.ep.observes)

    # ---- R3 比较:行下标 vs 常量
    def _compare(self, comp: ast.Compare) -> None:
        left, right = comp.left, comp.comparators[0] if len(comp.comparators) == 1 else None
        if right is None:
            return
        sub = _subscript_parts(left) or _subscript_parts(right)
        litv = _lit(right) or _lit(left)
        if sub and litv:
            row, attr = sub
            var = _normalize_var(row, attr)
            op = {ast.Eq: "eq", ast.NotEq: "ne", ast.In: "in"}.get(type(comp.ops[0]))
            if op:
                # 前置条件只对已知实体的状态原子成立;request_data[...] 是请求体
                # 可选旗标(如 VAmPI register 的 admin),不是"执行前置"——只留候选
                if _var_entity_known(var):
                    self.ep.preconditions.append(Precondition(
                        var=var, op=op, value=litv, file=self.file,
                        line=getattr(comp, "lineno", 0),
                    ))
                self._cand(var, "compare", f'{var} {op} "{litv}"', "condition")
        # ownership 比对:row["user_id"] vs user["id"]
        a, b = _subscript_parts(left), _subscript_parts(right)
        if a and b:
            var = _normalize_var(*a)
            self._cand(var, "compare", f'{a[0]}["{a[1]}"] vs {b[0]}["{b[1]}"]', "ownership_binding")
            if _var_entity_known(var):
                self.ep.preconditions.append(Precondition(
                    var=var, op="eq", key=f'{b[0]}.{b[1]}', file=self.file,
                    line=getattr(comp, "lineno", 0),
                ))

    # ---- R4 守卫:if 旗标/枚举/布尔 真值测试
    def _if(self, node: ast.If) -> None:
        test = node.test
        negated = isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
        inner = test.operand if negated else test
        if (sub := _subscript_parts(inner)):
            row, attr = sub
            var = _normalize_var(row, attr)
            op = "falsy" if negated else "truthy"
            # request_data[...] 等请求体真值测试只留候选,不产前置(register 的
            # 可选 admin 旗标不是"执行要求")
            if _var_entity_known(var):
                self.ep.preconditions.append(Precondition(
                    var=var, op=op, file=self.file, line=node.lineno,
                ))
            self._cand(var, "compare", f'if {"not " if negated else ""}{row}["{attr}"]', "condition")
        # owner/role 守卫(含旗标分支内)
        for comp in [n for n in ast.walk(node) if isinstance(n, ast.Compare)]:
            self._guard_from_compare(comp, node.lineno)

    def _guard_from_compare(self, comp: ast.Compare, line: int) -> None:
        if not (len(comp.ops) == 1 and isinstance(comp.ops[0], (ast.Eq, ast.NotEq))):
            return
        a = _subscript_parts(comp.left)
        b = _subscript_parts(comp.comparators[0])
        litv = _lit(comp.comparators[0]) or _lit(comp.left)
        ne = isinstance(comp.ops[0], ast.NotEq)
        new_guards: list[Guard] = []
        if a and b:
            (r1, f1), (r2, f2) = a, b
            if f1 == "user_id" or f2 == "user_id" or (f1 == "id" and r2 == "user") or (f2 == "id" and r1 == "user"):
                new_guards.append(Guard(
                    kind="owner", present=True, detail=f"{r1}.{f1} == {r2}.{f2}",
                    file=self.file, line=line,
                ))
            if "role" in (f1, f2) or "admin" in (f1, f2):
                new_guards.append(Guard(
                    kind="role", present=True,
                    detail=f"{r1}.{f1} {'!=' if ne else '=='} {r2}.{f2}",
                    file=self.file, line=line,
                ))
        elif a and litv:  # row["attr"] vs "常量"(role/status 前置)
            r1, f1 = a
            if f1 in ("role", "admin"):
                new_guards.append(Guard(
                    kind="role", present=True, detail=f'{r1}.{f1} {"!=" if ne else "=="} "{litv}"',
                    file=self.file, line=line,
                ))
            elif f1 == "status":
                new_guards.append(Guard(
                    kind="custom", present=True, detail=f'{r1}.status {"==" if ne else "!="} "{litv}"',
                    file=self.file, line=line,
                ))
        for g in new_guards:  # 端点内去重(嵌套 If 的重复 walk)
            if (g.kind, g.detail) not in {(x.kind, x.detail) for x in self.ep.guards}:
                self.ep.guards.append(g)

    # ---- R8:body = request.get_json(...) [or {}]
    def _assign(self, node: ast.Assign) -> None:
        if (
            len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            value = node.value
            if isinstance(value, ast.BoolOp):  # request.get_json(silent=True) or {}
                value = value.values[0]
            if isinstance(value, ast.Call) and _call_name(value).endswith(".get_json"):
                self.json_bodies.add(node.targets[0].id)
        # ORM 属性写:user.password = <expr> → Effect(仅已知行变量映射实体)
        if (
            len(node.targets) == 1
            and isinstance(node.targets[0], ast.Attribute)
            and isinstance(node.targets[0].value, ast.Name)
            and node.targets[0].value.id in _ROW_ENTITY
        ):
            attr_node = node.targets[0]
            var = _normalize_var(attr_node.value.id, attr_node.attr)
            self.ep.effects.append(Effect(
                var=var, op="set", file=self.file,
                line=getattr(node, "lineno", 0),
            ))
            self._cand(var, "write", ast.unparse(node)[:60], "db_field")
        # ORM 构造:newBook = Book(book_title=..., secret=...) → 每 kwarg 一条 Effect
        # (常量带值;request 取值 value=None —— 捕获 VAmPI register/books 的写事实)
        _ENTITY_BLACKLIST = {"Response", "Request", "Flask", "Blueprint", "App"}
        if (
            len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id[:1].isupper()
            and node.value.func.id not in _ENTITY_BLACKLIST
        ):
            entity = node.value.func.id
            for kw in node.value.keywords:
                if kw.arg is None:
                    continue
                value = (
                    str(kw.value.value)
                    if isinstance(kw.value, ast.Constant)
                    else None
                )
                var = f"{entity}.{kw.arg}"
                self.ep.effects.append(Effect(
                    var=var, op="set", value=value,
                    file=self.file, line=getattr(node, "lineno", 0),
                ))
                self._cand(var, "write", ast.unparse(node)[:60], "db_field")

    # ---- 候选登记(跨端点去重:self.cands 为文件级共享列表)
    def _cand(self, var: str, op: str, evidence: str, source: CandidateSource) -> None:
        for c in self.cands:
            if c.var == var:
                if op not in c.ops:
                    c.ops.append(op)
                return
        entity, _, attr = var.partition(".")
        self.cands.append(StateCandidate(
            var=var, entity=entity or None, attr=attr or None, ops=[op],
            evidence=evidence, source=source, file=self.file, line=0,
        ))


# ---------------------------------------------------------------- 路由/参数


def _route_of(node: ast.FunctionDef) -> list[tuple[str, list[HttpMethod]]]:
    out = []
    for dec in node.decorator_list:
        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
            attr = dec.func.attr
            if attr in ("get", "post", "put", "delete", "patch") and dec.args and _lit(dec.args[0]):
                out.append((dec.args[0].value, [attr.upper()]))  # type: ignore[list-item]
            elif attr == "route" and dec.args and _lit(dec.args[0]):
                methods: list[HttpMethod] = ["GET"]  # type: ignore[list-item]
                for kw in dec.keywords:
                    if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                        methods = [
                            e.value for e in kw.value.elts  # type: ignore[misc]
                            if isinstance(e, ast.Constant)
                        ]
                out.append((dec.args[0].value, methods))
    return out


def _path_params(route: str) -> list[Param]:
    params = []
    for m in re.finditer(r"<(?:int:|string:|path:)?(\w+)>", route):
        params.append(Param(name=m.group(1), location="path", py_type="str"))
    return params


def _call_name(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Attribute):
        base = f.value.id if isinstance(f.value, ast.Name) else (
            _call_name(f.value) if isinstance(f.value, ast.Call) else ""
        )
        return f"{base}.{f.attr}"
    return f.id if isinstance(f, ast.Name) else ""


def _is_os_getenv_int(call: ast.Call) -> bool:
    f = call.func
    inner = call
    if isinstance(f, ast.Name) and f.id == "int" and call.args:  # int(os.getenv(...))
        inner_arg = call.args[0]
        if isinstance(inner_arg, ast.Call):
            inner = inner_arg
            f = inner.func
    return (
        isinstance(f, ast.Attribute)
        and f.attr == "getenv"
        and isinstance(f.value, ast.Name)
        and f.value.id == "os"
        and bool(inner.args)
    )


def _getenv_default(call: ast.Call) -> int:
    target = call
    if isinstance(call.func, ast.Name) and call.func.id == "int" and call.args:
        target = call.args[0] if isinstance(call.args[0], ast.Call) else call
    if len(target.args) > 1 and isinstance(target.args[1], ast.Constant):
        try:
            return int(target.args[1].value)
        except (TypeError, ValueError):
            return 0
    for kw in target.keywords:
        if kw.arg == "default" and isinstance(kw.value, ast.Constant):
            try:
                return int(kw.value.value)
            except (TypeError, ValueError):
                return 0
    return 0


def _is_inline_json(node) -> bool:
    """(request.get_json(...) or {}).get / request.get_json(...).get 的链式形态。"""
    if isinstance(node, ast.BoolOp):
        return any(_is_inline_json(v) for v in node.values)
    if isinstance(node, ast.Call):
        return _call_name(node).endswith(".get_json")
    return False


_KNOWN_ENTITIES = {"Order", "Coupon", "User", "Book", "Session"}


def _var_entity_known(var: str) -> bool:
    return var.partition(".")[0] in _KNOWN_ENTITIES


def _dedup_by(items: list, key) -> list:
    seen: set = set()
    out = []
    for it in items:
        k = key(it)
        if k not in seen:
            seen.add(k)
            out.append(it)
    return out


def _bare_name(call: ast.Call) -> str:
    """直接函数名调用:jsonify(...) → 'jsonify';属性调用返回 ''。"""
    return call.func.id if isinstance(call.func, ast.Name) else ""


def _any_name(call: ast.Call) -> str:
    """裸名或属性名(忽略 base):user.encode_auth_token(...) → 'encode_auth_token'。"""
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def _is_concat(node) -> bool:
    return isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)


def _line(call, sql: str) -> int:
    return getattr(call, "lineno", 0) or 0


_ROW_ENTITY = {
    "order": "Order", "coupon": "Coupon", "user": "User", "book": "Book",
    "newBook": "Book", "order2": "Order", "coupon2": "Coupon", "target": "User",
    "newUser": "User", "found": "User",
}


def _normalize_var(row: str, attr: str) -> str:
    entity = _ROW_ENTITY.get(row)
    if entity is None:
        # 非已知行变量(request_data 等)不映射为实体
        return f"{row}.{attr}"
    return f"{entity}.{attr}"
