"""Session 访问事件提取（SS-01）与旧 M1 兼容层。

本模块有两条 API 线：

* 新 Session-first 线：``SessionAccess`` + ``extract_session_accesses``。
  以 BasicBlock 为锚（可选 ProgramIR），把源码中的 Session 读写记录为
  附着在代码位置上的 STATIC_MAY 访问事件；``extract_session_facts`` 是
  同源适配层，把同一批事件转成旧 ``StateFactCandidate``，避免两套结果
  各自扫描源码而漂移。
* 旧 Phase-4.3 M1 线：``analyze_session_access``（返回
  ``semantic_model.StateAccess``）保持原行为，供 state_seed_injector /
  transition / observer 等既有消费者使用，不在本批改动。

边界：第一版只识别 Flask 默认 Session 的常见形态（下标、get/pop/clear/
update、成员检查、增量赋值、del、简单别名与 import binding）。动态 key
保留 SYMBOLIC/UNKNOWN，不猜测；静态事件一律 ``effect_status=STATIC_MAY``，
永不进入运行时 verified 证据桶。
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from stateforge.state.semantic_model import (
    StateVariable, StateOp, StateAccess, StateSource)

# ------------------------------------------------------------------ 常量词表

BACKEND_FLASK_SESSION = "flask.session"
EFFECT_STATIC_MAY = "STATIC_MAY"

OP_READ = "READ"
OP_SET = "SET"
OP_DELETE = "DELETE"
OP_CLEAR = "CLEAR"
OP_READ_MODIFY_WRITE = "READ_MODIFY_WRITE"
OP_UNKNOWN = "UNKNOWN_ACCESS"

KEY_CONST = "CONST"
KEY_SYMBOLIC = "SYMBOLIC"
KEY_ANY = "ANY"
KEY_UNKNOWN = "UNKNOWN"

READ_GET = "GET"
READ_SUBSCRIPT = "SUBSCRIPT"
READ_MEMBERSHIP = "MEMBERSHIP"
READ_POP = "POP"
READ_MODIFY = "MODIFY"


@dataclass(frozen=True)
class SessionAccess:
    """一个 Session 访问点（位置级，非业务状态节点）。"""

    site_id: str
    block_id: str | None
    source_file: str
    source_line: int
    source_col: int
    operation: str
    backend: str = BACKEND_FLASK_SESSION
    key_kind: str = KEY_UNKNOWN
    key_expr: str | None = None
    value_expr: object = None
    effect_status: str = EFFECT_STATIC_MAY
    provenance: tuple[str, ...] = field(default_factory=tuple)
    read_kind: str | None = None
    default_expr: object = None
    default_present: bool = False
    reason: str = ""

    @property
    def key(self) -> str | None:
        """CONST key 的字面值；ANY/SYMBOLIC/UNKNOWN 返回 None。"""
        return self.key_expr if self.key_kind == KEY_CONST else None

    @property
    def canonical(self) -> str:
        if self.key_kind == KEY_CONST:
            return f"session.{self.key_expr}"
        if self.key_kind == KEY_ANY:
            return "session.*"
        return f"session.?{self.key_expr or 'unknown'}"

    def to_dict(self) -> dict:
        return {
            "site_id": self.site_id,
            "block_id": self.block_id,
            "source_file": self.source_file,
            "source_line": self.source_line,
            "source_col": self.source_col,
            "operation": self.operation,
            "backend": self.backend,
            "key_kind": self.key_kind,
            "key_expr": self.key_expr,
            "value_expr": self.value_expr,
            "effect_status": self.effect_status,
            "provenance": list(self.provenance),
            "read_kind": self.read_kind,
            "default_expr": self.default_expr,
            "default_present": self.default_present,
            "reason": self.reason,
        }


# ---------------------------------------------------------------- 提取环境

_LEGACY_NAME = "session"


def _unparse(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:                              # noqa: BLE001
        return None


def _const(node: ast.AST | None):
    if isinstance(node, ast.Constant):
        return node.value
    return None


def _walk_scope(fn: ast.AST):
    """遍历函数自身作用域;不进入嵌套函数/lambda(避免重复计访问)。"""
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _value_repr(node: ast.AST | None):
    """常量保留原生值；非常量保留可审计的符号表达式。"""
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        return node.value
    return f"?{_unparse(node)}"


def _key_of(node: ast.AST | None) -> tuple[str, str | None]:
    """AST 下标/参数 → (key_kind, key_expr)。未知 key 保留符号文本。"""
    if isinstance(node, ast.Constant):
        return KEY_CONST, str(node.value)
    if node is None:
        return KEY_UNKNOWN, None
    return KEY_SYMBOLIC, f"?{_unparse(node)}"


def _import_bindings(tree: ast.Module) -> dict[str, str]:
    """模块级 import → 本地名。只认 flask.session（不猜业务后端）。"""
    names: dict[str, str] = {}
    flask_module_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "flask":
            for alias in node.names:
                if alias.name == "session":
                    names[alias.asname or alias.name] = "flask.session"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "flask":
                    flask_module_names.add(alias.asname or alias.name)
    for n in flask_module_names:
        names[n] = "flask.module"
    return names


def _assigned_names(fn: ast.AST) -> set[str]:
    """函数内被普通赋值（非下标元素）遮蔽的名字。"""
    out: set[str] = set()
    for node in _walk_scope(fn):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out.add(t.id)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            t = node.target
            if isinstance(t, ast.Name):
                out.add(t.id)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            if isinstance(node.target, ast.Name):
                out.add(node.target.id)
        elif isinstance(node, ast.withitem) and isinstance(node.optional_vars, ast.Name):
            out.add(node.optional_vars.id)
    return out


def _is_session_expr(node: ast.AST | None, names: set[str],
                     flask_names: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return (node.value.id in flask_names and node.attr == "session")
    return False


def _name_provenance(node: ast.AST, names: set[str]) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (f"name:{node.id}",) if node.id in names else ()
    return ("attr:flask.session",)


def _session_names(fn: ast.AST, module_imports: dict[str, str]) -> tuple[set[str], set[str]]:
    """函数内有效的 session 名 + flask 模块名。

    兼容旧微靶的 ``def handler(session):``（无 import 但参数名就是
    session），同时排除 ``session = {}`` 这类局部遮蔽。import 别名优先；
    局部普通赋值会遮蔽同名 import/参数。
    """
    flask_names = {n for n, kind in module_imports.items()
                   if kind == "flask.module"}
    imported = {n for n, kind in module_imports.items()
                if kind == "flask.session"}
    args = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
    # 旧词汇：无 import 时参数/局部名 session 仍按 flask.session 处理。
    names = set(imported)
    if not imported and _LEGACY_NAME in args:
        names.add(_LEGACY_NAME)
    # 模块级 `session = flask.session` 赋值（旧靶/夹具常见）
    for node in _walk_scope(fn):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            if _is_session_expr(node.value, imported or {_LEGACY_NAME}, flask_names):
                names.add(node.targets[0].id)
    # 普通赋值/循环目标会遮蔽 import 或参数名；下标元素赋值不在
    # _assigned_names 中（`session["uid"]=...` 不遮蔽 session 本身）。
    names -= _assigned_names(fn)
    return names, flask_names


def _alias_session_names(fn: ast.AST, names: set[str],
                         flask_names: set[str]) -> dict[str, tuple[str, ...]]:
    """简单局部别名：``s = session`` / ``s = flask.session`` → 追加名字。"""
    aliases: dict[str, tuple[str, ...]] = {}
    for node in _walk_scope(fn):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and _is_session_expr(node.value, names, flask_names)):
            alias = node.targets[0].id
            aliases[alias] = (f"alias:{alias}<-{_unparse(node.value)}",)
    return aliases


# ---------------------------------------------------------------- 站点分配

class _SiteIds:
    """同一行/同一块的多个访问点用 (kind, seq) 区分，稳定且可溯源。"""

    def __init__(self, root: Path):
        self.root = root
        self._seen: dict[tuple[str, int, int, str], int] = {}

    def make(self, path: Path, lineno: int, col: int, kind: str) -> str:
        try:
            rel = path.resolve().relative_to(self.root.resolve())
        except (ValueError, OSError):
            rel = Path(path.name)
        base = (str(rel).replace("\\", "/"), lineno, col, kind)
        seq = self._seen.get(base, 0) + 1
        self._seen[base] = seq
        return f"{base[0]}:{lineno}:{col}:{kind}:{seq}"


# ---------------------------------------------------------------- 主提取器

@dataclass
class _Ctx:
    root: Path
    site_ids: _SiteIds
    block_lookup: Any = None

    def block(self, path: Path, lineno: int) -> str | None:
        if self.block_lookup is None:
            return None
        try:
            return self.block_lookup(str(path), lineno)
        except Exception:                          # noqa: BLE001
            return None


def _mk_access(ctx: _Ctx, path: Path, node: ast.AST,
               operation: str, key_kind: str, key_expr: str | None,
               *, kind: str, provenance: tuple[str, ...] = (),
               value_expr=None, read_kind: str | None = None,
               default_expr=None, default_present: bool = False,
               effect_status: str = EFFECT_STATIC_MAY,
               reason: str = "", col: int | None = None) -> SessionAccess:
    lineno = getattr(node, "lineno", 0)
    col_offset = getattr(node, "col_offset", 0) if col is None else col
    return SessionAccess(
        site_id=ctx.site_ids.make(path, lineno, col_offset, kind),
        block_id=ctx.block(path, lineno),
        source_file=str(path),
        source_line=lineno,
        source_col=col_offset,
        operation=operation,
        key_kind=key_kind,
        key_expr=key_expr,
        value_expr=value_expr,
        effect_status=effect_status,
        provenance=provenance,
        read_kind=read_kind,
        default_expr=default_expr,
        default_present=default_present,
        reason=reason,
    )


def _handle_call(ctx: _Ctx, path: Path, node: ast.Call,
                 names: set[str], flask_names: set[str],
                 provenance: tuple[str, ...]) -> list[SessionAccess]:
    func = node.func
    if not (isinstance(func, ast.Attribute)
            and _is_session_expr(func.value, names, flask_names)):
        return []
    method = func.attr
    out: list[SessionAccess] = []
    key_node = node.args[0] if node.args else None
    key_kind, key_expr = _key_of(key_node)
    default = node.args[1] if len(node.args) > 1 else None

    if method == "get":
        out.append(_mk_access(
            ctx, path, node, OP_READ, key_kind, key_expr,
            kind="get", provenance=provenance, read_kind=READ_GET,
            default_expr=_value_repr(default) if default is not None else None,
            default_present=default is not None))
    elif method == "pop":
        out.append(_mk_access(
            ctx, path, node, OP_READ, key_kind, key_expr,
            kind="pop-read", provenance=provenance, read_kind=READ_POP,
            default_expr=_value_repr(default) if default is not None else None,
            default_present=default is not None))
        out.append(_mk_access(
            ctx, path, node, OP_DELETE, key_kind, key_expr,
            kind="pop-delete", provenance=provenance, read_kind=READ_POP))
    elif method == "clear":
        out.append(_mk_access(
            ctx, path, node, OP_CLEAR, KEY_ANY, None,
            kind="clear", provenance=provenance))
    elif method == "update":
        arg = node.args[0] if node.args else None
        if isinstance(arg, ast.Dict):
            for i, (k, v) in enumerate(zip(arg.keys, arg.values)):
                kk, ke = _key_of(k)
                out.append(_mk_access(
                    ctx, path, node, OP_SET, kk, ke,
                    kind=f"update-{i}", provenance=provenance,
                    value_expr=_value_repr(v), col=node.col_offset))
        elif isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) \
                and arg.func.id == "dict" and arg.keywords:
            for i, kw in enumerate(arg.keywords):
                if kw.arg is None:
                    out.append(_mk_access(
                        ctx, path, node, OP_UNKNOWN, KEY_UNKNOWN, None,
                        kind=f"update-{i}", provenance=provenance,
                        effect_status="UNKNOWN",
                        reason="dynamic-mapping-update"))
                    continue
                out.append(_mk_access(
                    ctx, path, node, OP_SET, KEY_CONST, kw.arg,
                    kind=f"update-{i}", provenance=provenance,
                    value_expr=_value_repr(kw.value)))
        else:
            out.append(_mk_access(
                ctx, path, node, OP_UNKNOWN, KEY_UNKNOWN, None,
                kind="update-dynamic", provenance=provenance,
                effect_status="UNKNOWN", reason="dynamic-mapping-update"))
    elif method == "setdefault":
        out.append(_mk_access(
            ctx, path, node, OP_READ, key_kind, key_expr,
            kind="setdefault-read", provenance=provenance,
            read_kind=READ_GET, default_expr=_value_repr(node.args[1])
            if len(node.args) > 1 else None))
        out.append(_mk_access(
            ctx, path, node, OP_SET, key_kind, key_expr,
            kind="setdefault-set", provenance=provenance,
            value_expr=_value_repr(node.args[1])
            if len(node.args) > 1 else None))
    else:
        out.append(_mk_access(
            ctx, path, node, OP_UNKNOWN, KEY_UNKNOWN, None,
            kind=f"call-{method}", provenance=provenance,
            effect_status="UNKNOWN",
            reason=f"unsupported-session-method:{method}"))
    return out


def _extract_function(ctx: _Ctx, path: Path, fn: ast.AST,
                      module_imports: dict[str, str]) -> list[SessionAccess]:
    names, flask_names = _session_names(fn, module_imports)
    if not names and not flask_names:
        return []
    aliases = _alias_session_names(fn, names, flask_names)
    names |= set(aliases)
    out: list[SessionAccess] = []

    # 先记录下标/赋值/删除；Call 里的下标参数不重复算读。
    assign_targets: set[int] = set()
    aug_targets: set[int] = set()
    delete_targets: set[int] = set()
    for node in _walk_scope(fn):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                for sub in ast.walk(t):
                    if isinstance(sub, ast.Subscript):
                        assign_targets.add(id(sub))
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Subscript):
            aug_targets.add(id(node.target))
        elif isinstance(node, ast.Delete):
            for t in node.targets:
                for sub in ast.walk(t):
                    if isinstance(sub, ast.Subscript):
                        delete_targets.add(id(sub))

    for node in _walk_scope(fn):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Subscript) and \
                        _is_session_expr(t.value, names, flask_names):
                    kk, ke = _key_of(t.slice)
                    prov = _name_provenance(t.value, names)
                    if isinstance(t.value, ast.Name) and t.value.id in aliases:
                        prov = aliases[t.value.id]
                    # 下标元素赋值 → SET（不产生 READ）
                    out.append(_mk_access(
                        ctx, path, t, OP_SET, kk, ke,
                        kind="set", provenance=prov,
                        value_expr=_value_repr(node.value)))
        elif isinstance(node, ast.AugAssign):
            t = node.target
            if isinstance(t, ast.Subscript) and \
                    _is_session_expr(t.value, names, flask_names):
                kk, ke = _key_of(t.slice)
                out.append(_mk_access(
                    ctx, path, t, OP_READ_MODIFY_WRITE, kk, ke,
                    kind="augassign", provenance=_name_provenance(t.value, names),
                    value_expr=_value_repr(node.value)))
        elif isinstance(node, ast.Delete):
            for t in node.targets:
                if isinstance(t, ast.Subscript) and \
                        _is_session_expr(t.value, names, flask_names):
                    kk, ke = _key_of(t.slice)
                    out.append(_mk_access(
                        ctx, path, t, OP_DELETE, kk, ke,
                        kind="del", provenance=_name_provenance(t.value, names)))
        elif isinstance(node, ast.Subscript):
            if id(node) in assign_targets or id(node) in aug_targets \
                    or id(node) in delete_targets:
                continue
            if _is_session_expr(node.value, names, flask_names):
                kk, ke = _key_of(node.slice)
                if isinstance(node.ctx, ast.Load):
                    out.append(_mk_access(
                        ctx, path, node, OP_READ, kk, ke,
                        kind="subscript-read",
                        provenance=_name_provenance(node.value, names),
                        read_kind=READ_SUBSCRIPT))
        elif isinstance(node, ast.Call):
            prov = _name_provenance(node.func.value, names) \
                if isinstance(node.func, ast.Attribute) else ()
            out.extend(_handle_call(ctx, path, node, names, flask_names, prov))
        elif isinstance(node, ast.Compare):
            # 'key' in session / 'key' not in session
            for op, right in zip(node.ops, node.comparators):
                if isinstance(op, ast.In) and _is_session_expr(right, names, flask_names):
                    kk, ke = _key_of(node.left)
                    out.append(_mk_access(
                        ctx, path, node, OP_READ, kk, ke,
                        kind="membership", provenance=_name_provenance(right, names),
                        read_kind=READ_MEMBERSHIP))
                if isinstance(op, ast.NotIn) and _is_session_expr(right, names, flask_names):
                    kk, ke = _key_of(node.left)
                    out.append(_mk_access(
                        ctx, path, node, OP_READ, kk, ke,
                        kind="membership-not", provenance=_name_provenance(right, names),
                        read_kind=READ_MEMBERSHIP))

    # 同一行/同一列的 setdefault 两事件等已由 _SiteIds 的 kind 区分。
    return out


def extract_session_accesses(source_dir: str | Path,
                             ir=None, *,
                             block_lookup=None) -> list[SessionAccess]:
    """从源码目录提取全部 Session 访问事件（稳定 site_id，可选 BlockID）。"""
    root = Path(source_dir)
    if ir is not None and block_lookup is None:
        from stateforge.state.extractor import _BlockIndex
        block_lookup = _BlockIndex(ir).lookup
    ctx = _Ctx(root=root, site_ids=_SiteIds(root), block_lookup=block_lookup)
    out: list[SessionAccess] = []
    for py in sorted(root.rglob("*.py")):
        if "__pycache__" in str(py) or py.name.startswith("."):
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        module_imports = _import_bindings(tree)
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.extend(_extract_function(ctx, py, fn, module_imports))
    return sorted(out, key=lambda a: (a.source_file, a.source_line,
                                      a.source_col, a.site_id))


# ------------------------------------------------------ 同源适配到旧 StateFact

def session_facts_from_accesses(accesses: Iterable[SessionAccess]) -> list:
    """同一批 SessionAccess → 旧 StateFactCandidate（只做适配，不重扫）。"""
    from stateforge.state.model import (Relevance, StateDomain,
                                        StateFactCandidate, StateOperation)

    op_map = {
        OP_READ: StateOperation.READ,
        OP_SET: StateOperation.WRITE,
        OP_DELETE: StateOperation.DELETE,
        OP_CLEAR: StateOperation.DELETE,
        OP_READ_MODIFY_WRITE: StateOperation.WRITE,
        OP_UNKNOWN: StateOperation.READ,
    }
    out = []
    for a in accesses:
        if a.operation == OP_UNKNOWN:
            continue
        op = op_map[a.operation]
        ref = a.canonical
        raw = f"{a.site_id}|{a.operation}".encode("utf-8")
        cid = f"SA-{hashlib.sha256(raw).hexdigest()[:12]}"
        out.append(StateFactCandidate(
            candidate_id=cid,
            domain=StateDomain.SESSION,
            object_ref=ref,
            operation=op,
            source_file=a.source_file,
            function_name=None,
            lineno=a.source_line,
            block_id=a.block_id,
            predicate=f"{a.operation}({a.key_expr})",
            structured_predicate=None,
            written_value=(a.value_expr if a.operation in (OP_SET,)
                           and not isinstance(a.value_expr, str) else None),
            relevance=Relevance.UNKNOWN,
            evidence=(f"{a.source_file}:{a.source_line}:{a.source_col} "
                      f"{a.operation} {a.key_kind}",),
        ))
    return out


def extract_session_facts(source_dir: str | Path, ir=None):
    """返回 (SessionAccess 列表, 同源适配的 StateFactCandidate 列表)。"""
    accesses = extract_session_accesses(source_dir, ir=ir)
    return accesses, session_facts_from_accesses(accesses)


# ------------------------------------------------------- 旧 M1 兼容 API

def analyze_session_access(source_dir):
    """旧 Phase-4.3 M1 API（保持原行为，供既有消费者使用）。"""
    accesses = []
    for py in sorted(Path(source_dir).rglob("*.py")):
        if "__pycache__" in str(py):
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            accesses.extend(_legacy_visit_function(fn, str(py)))
    return accesses


def _legacy_visit_function(fn, filename):
    result = []
    for node in _walk_scope(fn):
        if isinstance(node, ast.Subscript) and _legacy_is_session(node.value):
            key = _legacy_extract_key(node.slice)
            if key is None:
                continue
            var = StateVariable(StateSource.SESSION, key)
            if isinstance(node.ctx, ast.Load):
                result.append(StateAccess(
                    variable=var, operation=StateOp.READ,
                    endpoint=None, function=fn.name,
                    filename=filename, lineno=node.lineno))
            elif isinstance(node.ctx, ast.Store):
                result.append(StateAccess(
                    variable=var, operation=StateOp.SET,
                    endpoint=None, function=fn.name,
                    filename=filename, lineno=node.lineno,
                    value_expr=_unparse(node)))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            obj = node.func.value
            method = node.func.attr
            if _legacy_is_session(obj) and method == "get" and node.args:
                key = _legacy_extract_key(node.args[0])
                if key:
                    result.append(StateAccess(
                        variable=StateVariable(StateSource.SESSION, key),
                        operation=StateOp.READ,
                        endpoint=None, function=fn.name,
                        filename=filename, lineno=node.lineno))
            if _legacy_is_session(obj) and method == "pop" and node.args:
                key = _legacy_extract_key(node.args[0])
                if key:
                    result.append(StateAccess(
                        variable=StateVariable(StateSource.SESSION, key),
                        operation=StateOp.DELETE,
                        endpoint=None, function=fn.name,
                        filename=filename, lineno=node.lineno))
            if _legacy_is_session(obj) and method == "clear":
                result.append(StateAccess(
                    variable=StateVariable(StateSource.SESSION, "*"),
                    operation=StateOp.CLEAR,
                    endpoint=None, function=fn.name,
                    filename=filename, lineno=node.lineno))
        if (isinstance(node, ast.AugAssign)
                and isinstance(node.target, ast.Subscript)
                and _legacy_is_session(node.target.value)):
            key = _legacy_extract_key(node.target.slice)
            if key:
                result.append(StateAccess(
                    variable=StateVariable(StateSource.SESSION, key),
                    operation=StateOp.UPDATE,
                    endpoint=None, function=fn.name,
                    filename=filename, lineno=node.lineno))
        if isinstance(node, ast.Delete):
            for target in node.targets:
                if (isinstance(target, ast.Subscript)
                        and _legacy_is_session(target.value)):
                    key = _legacy_extract_key(target.slice)
                    if key:
                        result.append(StateAccess(
                            variable=StateVariable(StateSource.SESSION, key),
                            operation=StateOp.DELETE,
                            endpoint=None, function=fn.name,
                            filename=filename, lineno=node.lineno))
    return result


def _legacy_is_session(obj):
    return isinstance(obj, ast.Name) and obj.id == _LEGACY_NAME


def _legacy_extract_key(node):
    if isinstance(node, ast.Constant):
        return str(node.value)
    return None
