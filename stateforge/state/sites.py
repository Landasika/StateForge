"""状态改变点 / 守卫点的通用静态提取(零框架词典)。

设计契约(2026-11-10 讨论):

- 一套**语言形状**规则,不出现任何框架名:
  * store 写:``x[k] = v`` / ``del x[k]`` / ``x.pop(k)`` /
    ``x.setdefault(k, v)`` / ``x.update(...)`` / ``x.clear()`` / ``x.flush()``,
    其中 ``x`` 是**跨函数共享**的下标基链。共享判定 = 同一链出现在
    ≥ ``min_functions`` 个不同函数——这是"跨请求可变状态"的纯静态
    近似:单函数局部字典不是持久状态,不靠名字判断,靠使用分布。
  * 属性写:``x.attr = v``(如 ``session.permanent = True``)。**仅当
    该链在扫描范围内别处有下标/变更证据**才记为转移点——属性接口
    本身不证明对象是 store(``self.title = ...`` 到处都是),不猜。
  * 守卫:``if <cond>: return/raise``(提前退出形状)。只认形状,
    不解释语义;``not`` 前缀单独记极性。
- key 解析:同文件模块级/类级常量折叠(Django 的 ``SESSION_KEY`` →
  ``"_auth_user_id"`` 即由这条命中)。解不出的一律显式标注
  (``<dyn>`` / ``<name:...>``),绝不猜——与 URLconf 短名教训同一纪律。
- **原子不透明**:不产出 "auth"/"登录" 之类语义名;slot 就是
  ``链["key"]``。语义命名是人的便利,不是机器的必需。
- **概念分三类,不混**(2026-11-10 修正):
  * ``StoreSite`` = **状态转移点**:那条语句本身,执行即改变持久
    状态(``session["id"] = user.id`` 就是转移点,不锚定"两端")。
  * ``GuardSite`` = **守卫**:读侧事实("这段代码需要什么状态才能
    过"),与"谁改变了状态"是两类事实;规划时 join 使用。
  * 转移**记录**(边)由动态层派生:请求执行到转移点前后各拍快照,
    差即 from→to;静态层只给点,不给边。

分层(L1.5 state):仅 stdlib + dataclass,零本包依赖。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

# ---------------------------------------------------------------- 产物


@dataclass(frozen=True)
class StoreSite:
    """一个跨函数共享 store 的写/删/变更点。"""

    chain: str            # 基链:"request.session" / "session" / ...
    key: str | None       # 解析后的 key;None = 整库操作(clear/flush)
    key_status: str       # literal | const | name | dyn | attr | none
    op: str               # write | delete | pop | setdefault | update | clear | flush
    value: str | None     # 写入值的源码(截断);删除/清空为 None
    file: str
    line: int
    function: str         # 最近包围函数("<module>" 表示模块级)
    head_is_param: bool = False   # 链头是所在函数的形参(入口流入)
    head_is_import: bool = False  # 链头是本文件模块级导入名(如 flask 的代理)

    @property
    def slot(self) -> str:
        """不透明原子:链 + key。key 未知时保留标注,不猜。"""
        return f'{self.chain}[{self.key}]' if self.key is not None else f'{self.chain}[*]'


@dataclass(frozen=True)
class GuardSite:
    """一个提前退出守卫:过此点要求 <cond> 为真(或非真,若 negated)。"""

    expr: str             # 条件源码(截断)
    negated: bool         # 顶层 ``not`` 前缀
    roots: tuple[str, ...]  # 条件引用的根符号(参数名自然涌现,非词典)
    file: str
    line: int
    function: str


@dataclass(frozen=True)
class SiteScan:
    stores: tuple[StoreSite, ...]
    guards: tuple[GuardSite, ...]
    dropped_unshared: int   # 因"仅单函数使用"被丢弃的候选 store 数


# ---------------------------------------------------------------- 形状工具

_MUT_OPS = {"pop", "setdefault", "update", "clear", "flush"}
_KEYLESS = {"clear", "flush"}


def _chain(node: ast.AST) -> str | None:
    """Name/Attribute 链 → 点串;含下标等复杂结构返回 None。"""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _roots(node: ast.AST, acc: set[str]) -> set[str]:
    """表达式根符号:Name 本体、属性链起点。函数名(len/isinstance)同样
    会涌现——它们无害,由消费方按需过滤。"""
    if isinstance(node, ast.Name):
        acc.add(node.id)
    for child in ast.iter_child_nodes(node):
        _roots(child, acc)
    return acc


def _const_table(tree: ast.Module) -> dict[str, str]:
    """同文件常量表:单目标 ``NAME = <常量>`` / ``NAME: T = <常量>``。

    函数内同名赋值会遮蔽模块常量——启发式近似,对框架常量
    (SESSION_KEY 这类全大写模块常量)足够准;错配由 key_status
    可追溯("const" 即可查表复核)。
    """
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        value = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            value = node.value
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            value = node.value
            target = node.target
        else:
            continue
        if isinstance(target, ast.Name) and isinstance(
                value, (ast.Constant,)) and isinstance(value.value, (str, int, bool)):
            out.setdefault(target.id, str(value.value))
    return out


def _resolve_key(node: ast.AST | None, consts: dict[str, str]) -> tuple[str | None, str]:
    """下标 key / 方法首参 → (解析值, 证据等级)。解不出显式标注。"""
    if node is None:
        return None, "none"
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, bool)):
        return str(node.value), "literal"
    if isinstance(node, ast.Name):
        if node.id in consts:
            return consts[node.id], "const"
        return f"<name:{node.id}>", "name"
    return "<dyn>", "dyn"      # BinOp/JoinedStr/Subscript/... 一律不猜


# ---------------------------------------------------------------- 提取


class _Visitor(ast.NodeVisitor):
    """单文件:记录候选 store(未过滤)与守卫;函数栈限定归属。"""

    def __init__(self, file: str, consts: dict[str, str],
                 module_imports: set[str]):
        self.file = file
        self.consts = consts
        self.module_imports = module_imports
        self.fn_stack: list[str] = []
        self.params: set[str] = set()
        self.stores: list[StoreSite] = []
        self.attr_writes: list[StoreSite] = []   # 属性写候选(待证据门)
        self.guards: list[GuardSite] = []
        self.usage: dict[str, set[str]] = {}   # chain -> {函数全名}

    def _fn(self) -> str:
        return ".".join(self.fn_stack) if self.fn_stack else "<module>"

    # -- 函数/类栈:归属 + 参数集(嵌套函数各自独立,出栈恢复外层) --
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.fn_stack.append(node.name)
        self.generic_visit(node)
        self.fn_stack.pop()

    def visit_FunctionDef(self, node) -> None:
        saved = self.params
        self.fn_stack.append(node.name)
        args = node.args
        self.params = ({a.arg for a in getattr(args, "posonlyargs", [])}
                       | {a.arg for a in args.args}
                       | {a.arg for a in args.kwonlyargs})
        if args.kwarg:
            self.params.add(args.kwarg.arg)
        self.generic_visit(node)
        self.fn_stack.pop()
        self.params = saved

    visit_AsyncFunctionDef = visit_FunctionDef

    # -- store 写 --
    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Subscript):
                self._record(target.value, target.slice, "write",
                             ast.unparse(node.value)[:60], node.lineno)
            elif isinstance(target, ast.Attribute):
                self._record_attr(target, node.value, node.lineno)
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            if isinstance(target, ast.Subscript):
                self._record(target.value, target.slice, "delete",
                             None, node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute) and node.func.attr in _MUT_OPS:
            key_node = node.args[0] if node.args else None
            op = node.func.attr
            if op in _KEYLESS:
                key_node = None
            self._record(node.func.value, key_node, op, None, node.lineno)
        self.generic_visit(node)

    def _record(self, base: ast.AST, key_node, op: str,
                value: str | None, lineno: int) -> None:
        chain = _chain(base)
        if chain is None:
            return
        key, status = _resolve_key(key_node, self.consts)
        head = chain.split(".", 1)[0]
        self.stores.append(StoreSite(
            chain=chain, key=key, key_status=status, op=op,
            value=value, file=self.file, line=lineno, function=self._fn(),
            head_is_param=head in self.params,
            head_is_import=head in self.module_imports))
        self.usage.setdefault(chain, set()).add(self._fn())

    def _record_attr(self, target: ast.Attribute, value_node, lineno) -> None:
        """属性写候选(x.attr = v)。仅记录;入列与否由
        ``extract_sites`` 的下标证据门决定(见模块 docstring)。
        """
        chain = _chain(target.value)
        if chain is None:
            return
        head = chain.split(".", 1)[0]
        self.attr_writes.append(StoreSite(
            chain=chain, key=target.attr, key_status="attr", op="write",
            value=ast.unparse(value_node)[:60], file=self.file,
            line=lineno, function=self._fn(),
            head_is_param=head in self.params,
            head_is_import=head in self.module_imports))
        self.usage.setdefault(chain, set()).add(self._fn())
        self.usage.setdefault(chain, set()).add(self._fn())

    # -- 守卫 --
    def visit_If(self, node: ast.If) -> None:
        if any(isinstance(s, (ast.Return, ast.Raise)) for s in node.body):
            test = node.test
            negated = isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
            self.guards.append(GuardSite(
                expr=ast.unparse(test)[:80], negated=negated,
                roots=tuple(sorted(_roots(test, set()))),
                file=self.file, line=node.lineno, function=self._fn()))
        self.generic_visit(node)


def _iter_files(roots: Iterable[Path], skip: Callable[[Path], bool] | None):
    for root in roots:
        for p in sorted(root.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            if skip and skip(p):
                continue
            yield p


def _module_imports(tree: ast.Module) -> set[str]:
    """本文件模块级导入名(``from flask import session`` 的 session)。"""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                out.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.Import):
            for a in node.names:
                out.add(a.asname or a.name.split(".")[0])
    return out


def make_entry_matcher(entries) -> Callable[[str, str], bool]:
    """由端点索引 ``(文件名, 函数名)`` 构造 ``is_entry``,CBV 感知。

    类视图方法在本提取器里的函数全名是 ``Class.method``(函数栈压入
    类名),而端点侧登记的是类名(如 linkding 的
    ``django_auth_views.PasswordChangeDoneView``)。直接逐字匹配会把
    类视图的每个方法都判成非入口,连锁头 ``self`` 的
    ``self.request.session`` 全部失去资格。规则:函数名按 ``.`` 去掉
    最后一段后命中端点类名(且文件相同)即视为入口——过近似
    (辅助方法也算),但对"store 资格"这粒度是安全方向。
    """
    entry_set = set(entries)

    def is_entry(function: str, file: str) -> bool:
        base = Path(file).name
        if (base, function) in entry_set:
            return True
        cls, sep, _method = function.rpartition(".")
        return bool(sep) and (base, cls) in entry_set

    return is_entry


def extract_sites(roots: Iterable[Path | str], *,
                  skip: Callable[[Path], bool] | None = None,
                  min_functions: int = 2,
                  is_entry: Callable[[str, str], bool] | None = None
                  ) -> SiteScan:
    """扫描 app(+可选框架切片)→ 共享 store 位点 + 守卫位点。

    ``min_functions``:一条链被视为共享 store 的最少使用函数数。
    跨函数共享是"跨请求状态"的静态影子——框架代码(django 等)与
    应用代码放在同一 ``roots`` 里联合判定。

    ``is_entry(function, file)``:入口函数判定(如端点视图,可用
    ``make_entry_matcher`` 构造)。提供时,共享链还须**有资格**:
    链头是某个入口函数的形参(``request.session`` ← 框架以参数流入
    视图),或是模块级导入名(flask 的 ``session`` 代理)。这把
    ``kwargs``/``context`` 这类跨函数但请求内局部的字典排除掉——
    不靠名字,靠"身份是否可能活得比一次请求久"。
    不提供时退回纯共享判定(全部函数视为潜在入口)。

    属性写(``x.attr = v``)过**下标证据门**:仅当同一链在扫描范围内
    有下标/变更证据(证明对象是字典式 store)才并入转移点。
    """
    roots = [Path(r) for r in roots]
    guards: list[GuardSite] = []
    visitors: list[_Visitor] = []
    seen_files: set[Path] = set()      # 多根重叠(app 根 ⊇ 框架切片)只扫一次
    for p in _iter_files(roots, skip):
        rp = p.resolve()
        if rp in seen_files:
            continue
        seen_files.add(rp)
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        v = _Visitor(str(p), _const_table(tree), _module_imports(tree))
        v.visit(tree)
        visitors.append(v)
        guards.extend(v.guards)

    # 下标/变更证据链(属性写并入的门)
    evidenced = {s.chain for v in visitors for s in v.stores}
    stores: list[StoreSite] = []
    usage: dict[str, set[str]] = {}
    by_chain: dict[str, list[StoreSite]] = {}
    for v in visitors:
        v.stores.extend(a for a in v.attr_writes if a.chain in evidenced)
        stores.extend(v.stores)
        for s in v.stores:
            by_chain.setdefault(s.chain, []).append(s)
        for chain, fns in v.usage.items():
            usage.setdefault(chain, set()).update(fns)
    shared = {c for c, fns in usage.items() if len(fns) >= min_functions}
    if is_entry is not None:
        entry_cache: dict[tuple[str, str], bool] = {}

        def _entry(fn: str, file: str) -> bool:
            k = (fn, file)
            if k not in entry_cache:
                entry_cache[k] = is_entry(fn, file)
            return entry_cache[k]

        def _qualified(s: StoreSite) -> bool:
            return ((s.head_is_param and _entry(s.function, s.file))
                    or s.head_is_import)

        shared = {c for c in shared
                  if any(_qualified(s) for s in by_chain.get(c, ()))}
    kept = tuple(s for s in stores if s.chain in shared)
    return SiteScan(
        stores=kept, guards=tuple(guards),
        dropped_unshared=len(stores) - len(kept))
