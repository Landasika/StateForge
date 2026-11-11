"""django_adapter.py —— T+13:Django 路由适配(URLconf → 端点)。

覆盖:urlpatterns 的 path()/re_path();include("app.urls") 递归(前缀拼接);
函数视图(views.detail);类视图(View.as_view() → get/post 方法展开);
admin.site.urls / 第三方 include(allauth 等)静默跳过(ext 不入图)。
路径转换器 "<int:x>" 复用 _path_params。
"""

from __future__ import annotations

import ast
from pathlib import Path

from stateforge.analysis.frontend.pyslice import venv_site_packages
from stateforge.analysis.patterns import EndpointExtractor, _path_params
from stateforge.core.model import AppModel, Endpoint

_SKIP_DIRS = {"__pycache__", "migrations", "docs", "dockerized_labs", "chatbot",
              "node_modules", ".git", "tests", ".venv", "venv", ".tox", ".eggs",
              "site-packages"}


def _literal(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _load_defs(app_root: Path):
    """(模块名|短名, 函数/类名) → 节点 注册表。"""
    funcs: dict[tuple[str, str], ast.FunctionDef] = {}
    classes: dict[tuple[str, str], ast.ClassDef] = {}
    for py in sorted(app_root.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in py.parts):
            continue
        module = ".".join(py.with_suffix("").parts[len(app_root.parts):])
        short = py.with_suffix("").name
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            # AST nodes retain their defining source, independently of the
            # URLconf that imports/registers them.
            node._sf_source_file = str(py)
            if isinstance(node, ast.FunctionDef):
                funcs.setdefault((module, node.name), node)
                funcs.setdefault((short, node.name), node)
            elif isinstance(node, ast.ClassDef):
                classes.setdefault((module, node.name), node)
                classes.setdefault((short, node.name), node)
    return funcs, classes


def _function_view_methods(fdef) -> list[str]:
    """HTTP methods a function-based view actually accepts.

    Returning a blanket ``["GET"]`` hid POST-only views: linkding's ``login``
    is a function view whose body branches on ``request.method == "POST"``, so
    a pool built from the analysis never contained ``POST /login/`` and no
    search could ever reach the login session write.

    Two cases, because Django treats function views differently from Flask:

    * explicit ``require_POST`` / ``require_http_methods`` decorators state the
      exact set, so they are authoritative;
    * a plain function view is dispatched for *every* method unless it guards
      ``request.method`` itself. linkding's ``edit`` renders on GET and saves
      on POST, so it must publish both.

    Reads ``request.method == "POST"`` / ``!= "GET"`` style comparisons to add
    methods, and never narrows below GET for an unguarded view.
    """
    if fdef is None:
        return ["GET"]
    explicit: list[str] = []
    guarded: list[str] = []
    verbs = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
    for node in ast.walk(fdef):
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            for side in (node.left, node.comparators[0]):
                if isinstance(side, ast.Constant) and isinstance(side.value, str) \
                        and side.value.upper() in verbs:
                    verb = side.value.upper()
                    if isinstance(node.ops[0], ast.Eq) and verb not in guarded:
                        guarded.append(verb)
                    elif isinstance(node.ops[0], ast.NotEq) and verb == "GET" \
                            and "POST" not in guarded:
                        guarded.append("POST")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            name = node.func.id
            if name.startswith("require_") and name[8:].upper() in verbs:
                verb = name[8:].upper()
                if verb not in explicit:
                    explicit.append(verb)
    if explicit:
        return explicit
    # A function view accepts every method unless it branches on request.method
    # or is restricted by a decorator; GET is always included.
    return ["GET"] + [m for m in guarded if m != "GET"]


def _class_view(cls: ast.ClassDef, name: str):
    methods = [m.upper() for m in ("get", "post", "put", "delete", "patch")
               if any(isinstance(s, ast.FunctionDef) and s.name == m
                      for s in ast.walk(cls))]
    fdef = next((s for s in cls.body if isinstance(s, ast.FunctionDef)
                 and s.name in ("get", "post")), None)
    return (methods or ["GET"], fdef, name)


def _urlconf_imports(tree: ast.Module, app_root: Path) -> dict[str, str]:
    """该 URLconf 的 ``本地名 -> 完整模块路径`` 表。

    URLconf 常用 ``from bookmarks.views import bundles as bundles_views`` 这类
    别名注册视图。别名本身不是文件名,拿它去按短名猜会张冠李戴——linkding 的
    ``bundles/<int:bundle_id>/edit`` 就是这样被记成了 ``bookmarks.py`` 里的
    ``edit``。
    """
    root = app_root.resolve()
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                out[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    out[alias.asname] = alias.name
    # A relative import would need package context we do not track here; such
    # entries are simply absent, which makes the caller refuse rather than guess.
    del root
    return out


def _unique_by_name(name: str, table: dict) -> object | None:
    """按名字全项目查;只有唯一候选时才接受。

    旧实现取 ``next(...)`` 的第一个命中,于是任意同名函数都可能被认成目标
    ——这是"看起来解析成功、实际指错位置"的假匹配。宁可返回 None。
    """
    hits = {id(node): node for (module, candidate), node in table.items()
            if candidate == name}
    return next(iter(hits.values())) if len(hits) == 1 else None


def _view_ref(node, funcs, classes, imports: dict[str, str] | None = None,
              ext: "_ExtDefs | None" = None):
    """view 参数 → (methods, FunctionDef|None, 显示名);解析不了返回 None。

    ``ext``:venv 惰性 def 索引。URLconf include 解进 venv 后,视图也
    定义在 venv(wagtail/django.contrib.auth);应用注册表查不到时按
    候选模块名取 venv 定义,**仍然只认能精确命中的,不猜**。
    """
    imports = imports or {}

    def _ext_hit(module: str, name: str):
        if ext is None:
            return None
        defs = ext.module_defs(module)
        if defs and name in defs:
            node_def = defs[name]
            if isinstance(node_def, ast.ClassDef):
                return _class_view(node_def, name)
            return _function_view_methods(node_def), node_def, name
        return None
    if isinstance(node, ast.Name):                     # 直接函数名(同模块 import)
        f = _unique_by_name(node.id, funcs)
        return (_function_view_methods(f), f, node.id) if f else None
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        alias, attr = node.value.id, node.attr
        # 1) 别名经该 URLconf 的导入表解析成完整模块(唯一可靠路径)
        target = imports.get(alias)
        if target:
            submodule = target.rsplit(".", 1)[0] if target.rsplit(".", 1)[-1] != attr \
                else target
            for candidate in (target, f"{target}.{attr}", submodule):
                if (candidate, attr) in funcs:
                    f = funcs[(candidate, attr)]
                    return _function_view_methods(f), f, attr
                if (candidate, attr) in classes:
                    return _class_view(classes[(candidate, attr)], attr)
                hit = _ext_hit(candidate, attr)
                if hit is not None:
                    return hit
            # 别名已确定却查不到成员:第三方视图,拒绝而不是猜
            if (alias, attr) not in funcs and (alias, attr) not in classes:
                return None
        key = (alias, attr)                            # 文件名/模块名直接命中
        if key in funcs:
            return _function_view_methods(funcs[key]), funcs[key], attr
        if key in classes:
            return _class_view(classes[key], attr)
        f = _unique_by_name(attr, funcs)
        return (_function_view_methods(f), f, attr) if f else None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr == "as_view":           # View.as_view()
        inner = node.func.value
        name = None
        cls = None
        if isinstance(inner, ast.Name):
            name = inner.id
            cls = next((c for (m, n), c in classes.items() if n == inner.id), None)
        elif isinstance(inner, ast.Attribute) and isinstance(inner.value, ast.Name):
            name = inner.attr
            cls = classes.get((inner.value.id, inner.attr))
            if cls is None:
                cls = next((c for (m, n), c in classes.items() if n == name), None)
        if cls is not None:
            return _class_view(cls, name or "<view>")
    return None


def _parse_urlpatterns(tree: ast.Module) -> list:
    """urlpatterns 全量收集:首赋值、``+=`` 拼接、``i18n_patterns`` 包裹。

    只认第一条赋值的旧实现漏了两类真实写法:oscar 的
    ``urlpatterns += i18n_patterns(...)``(主体路由全在里面,漏读即
    0 端点)与 zinnia 的多段 ``urlpatterns += [...]``。
    """
    out: list = []

    def _add(value):
        if isinstance(value, (ast.List, ast.Tuple)):
            out.extend(value.elts)
        elif (isinstance(value, ast.Call)
              and isinstance(value.func, ast.Name)
              and value.func.id == "i18n_patterns"):
            for arg in value.args:
                if isinstance(arg, (ast.List, ast.Tuple)):
                    out.extend(arg.elts)
                else:
                    out.append(arg)

    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target])
            if any(isinstance(t, ast.Name) and t.id == "urlpatterns"
                   for t in targets):
                _add(node.value)
    return out


def _iter_urlconf_files(app_root: Path) -> list[Path]:  # noqa: D401
    """应用根下的 URLconf 文件:``urls.py`` 与 ``urls`` **包**
    (``urls/__init__.py``——zinnia 的 ``zinnia.urls`` 就是包,
    只 rglob("urls.py") 会整个漏掉)。
    """
    out: list[Path] = []
    for p in sorted(app_root.rglob("urls.py")):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        out.append(p)
    for p in sorted(app_root.rglob("urls/*.py")):      # urls 包的子模块
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        out.append(p)
    return list(dict.fromkeys(out))


def _module_key(p: Path, app_root: Path) -> str:
    """URLconf 文件 → 点分模块名:urls.py/子模块.py 去后缀,
    urls/__init__.py 取目录(即 urls 包本身)。"""
    rel = p.parent if p.name == "__init__.py" else p.with_suffix("")
    return ".".join(rel.parts[len(app_root.parts):])


def _venv_site(app_root: Path) -> Path | None:
    """The virtualenv's ``site-packages``, however the venv is named.

    Discovery is shared with the ICFG dependency slice, so a project keeping
    its environment in ``env/``, ``venv/`` or a custom directory resolves
    ``include("pkg.urls")`` just like one using ``.venv``.
    """
    sites = venv_site_packages(app_root)
    return sites[0] if sites else None


def _venv_module_file(sp: Path, dotted: str) -> Path | None:
    """点分模块名 → venv 文件(按点分段:``wagtail.urls`` → wagtail/urls.py)。"""
    base = sp.joinpath(*dotted.split("."))
    for cand in (base.with_suffix(".py"), base / "__init__.py"):
        if cand.is_file():
            return cand
    return None


class _ExtDefs:
    """venv 里的 def 惰性索引:按需解析单个模块文件,不全量扫依赖树。

    URLconf include 解进 venv 后,视图也定义在 venv(如 wagtail);
    只解析真正被引用的模块文件,每文件一次。
    """

    def __init__(self, site_packages: Path | None):
        self.sp = site_packages
        self._cache: dict[str, dict | None] = {}

    def module_defs(self, dotted: str) -> dict | None:
        if dotted in self._cache:
            return self._cache[dotted]
        result = None
        if self.sp is not None:
            cand = _venv_module_file(self.sp, dotted)
            if cand is not None:
                try:
                    tree = ast.parse(cand.read_text(encoding="utf-8"))
                except (OSError, SyntaxError):
                    tree = None
                if tree is not None:
                    defs: dict = {}
                    for node in ast.walk(tree):
                        node._sf_source_file = str(cand)
                        if isinstance(node, ast.FunctionDef):
                            defs[node.name] = node
                        elif isinstance(node, ast.ClassDef):
                            defs[node.name] = node
                    result = defs
        self._cache[dotted] = result
        return result

    def parse_module_tree(self, dotted: str) -> ast.Module | None:
        """按模块名解析整棵 URLconf 树(include 递归用)。"""
        if self.sp is None:
            return None
        cand = _venv_module_file(self.sp, dotted)
        if cand is None:
            return None
        try:
            return ast.parse(cand.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            return None


def extract_django(app_root: Path) -> AppModel:
    model = AppModel(app_root=str(app_root), framework="django")
    ex = EndpointExtractor(str(app_root), model.config_flags)
    funcs, classes = _load_defs(app_root)

    trees: dict[str, ast.Module] = {}
    for p in _iter_urlconf_files(app_root):
        try:
            trees[_module_key(p, app_root)] = ast.parse(
                p.read_text(encoding="utf-8"), filename=str(p))
        except SyntaxError:
            continue
    by_short = {key.rsplit(".", 1)[0]: t for key, t in trees.items()}
    ext = _ExtDefs(_venv_site(app_root))

    def walk(els: list, prefix: str, origin: Path, imports: dict[str, str],
             depth: int = 0):
        for elt in els:
            if not (isinstance(elt, ast.Call)
                    and isinstance(elt.func, (ast.Name, ast.Attribute))):
                continue
            fname = (elt.func.id if isinstance(elt.func, ast.Name)
                     else elt.func.attr)
            if fname not in ("path", "re_path", "url"):   # url=老式(Django<4)
                continue
            route = _literal(elt.args[0]) if elt.args else ""
            view = elt.args[1] if len(elt.args) > 1 else None
            full = prefix + (route or "")
            if isinstance(view, ast.Call) and isinstance(view.func, ast.Name) \
                    and view.func.id == "include":
                _include(view.args[0] if view.args else None,
                         full, origin, imports, depth)
                continue
            if isinstance(view, ast.Call) and isinstance(view.func, ast.Attribute) \
                    and view.func.attr == "urls":
                continue                                  # admin.site.urls 等
            if view is None:
                continue
            ref = _view_ref(view, funcs, classes, imports, ext=ext)
            if ref is None:
                continue                                  # admin.site.urls 等
            methods, fdef, name = ref
            source_file = getattr(fdef, "_sf_source_file", str(origin))
            ep = Endpoint(func=name, route=full, methods=methods,
                          params=_path_params(full), file=source_file,
                          line=(fdef.lineno if fdef else 0))
            if fdef is not None:
                ex.file = source_file
                ex._extract_endpoint_into(fdef, full, methods, ep)
            model.endpoints.append(ep)

    def _resolve_include_dotted(arg, imports: dict[str, str]) -> str | None:
        """include 参数 → 点分模块名:字面量 / 变量(经导入表) / 属性链。"""
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
        if isinstance(arg, ast.Name):
            return imports.get(arg.id)
        if isinstance(arg, ast.Attribute):
            parts = []
            node = arg
            while isinstance(node, ast.Attribute):
                parts.append(node.attr)
                node = node.value
            if isinstance(node, ast.Name):
                base = imports.get(node.id, node.id)
                return ".".join([base] + list(reversed(parts)))
        return None

    def _include(arg, full: str, origin: Path, imports: dict[str, str],
                 depth: int = 0) -> None:
        target = _resolve_include_dotted(arg, imports)
        if not target or depth > 8 or target in visited:
            return
        sub = (trees.get(target)
               or trees.get(target.rstrip("/") + ".urls")
               or trees.get(target + ".urls"))
        sub_imports = imports
        if sub is None:
            sub = ext.parse_module_tree(target)
            if sub is None:
                sub = ext.parse_module_tree(target.rstrip("/") + ".urls")
            if sub is not None:
                sub_imports = _urlconf_imports(sub, app_root)
        else:
            sub_imports = _urlconf_imports(sub, app_root)
        if sub is None:
            return
        visited.add(target)
        for elt in _parse_urlpatterns(sub):
            _walk_elt(elt, full, origin, sub_imports, depth + 1)

    def _walk_elt(elt, prefix: str, origin: Path, imports: dict[str, str],
                  depth: int = 0) -> None:
        walk([elt], prefix, origin, imports, depth)

    visited: set[str] = set()
    for key, tree in trees.items():
        visited.add(key)
        walk(_parse_urlpatterns(tree), "", Path(key), _urlconf_imports(tree, app_root))
    model.candidates.extend(ex.candidates)
    model.config_flags.update(ex.config_flags)
    return model
