"""django_adapter.py —— T+13:Django 路由适配(URLconf → 端点)。

覆盖:urlpatterns 的 path()/re_path();include("app.urls") 递归(前缀拼接);
函数视图(views.detail);类视图(View.as_view() → get/post 方法展开);
admin.site.urls / 第三方 include(allauth 等)静默跳过(ext 不入图)。
路径转换器 "<int:x>" 复用 _path_params。
"""

from __future__ import annotations

import ast
from pathlib import Path

from stateforge.analysis.patterns import EndpointExtractor, _path_params
from stateforge.core.model import AppModel, Endpoint

_SKIP_DIRS = {"__pycache__", "migrations", "docs", "dockerized_labs", "chatbot",
              "node_modules", ".git", "tests"}


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
            if isinstance(node, ast.FunctionDef):
                funcs.setdefault((module, node.name), node)
                funcs.setdefault((short, node.name), node)
            elif isinstance(node, ast.ClassDef):
                classes.setdefault((module, node.name), node)
                classes.setdefault((short, node.name), node)
    return funcs, classes


def _class_view(cls: ast.ClassDef, name: str):
    methods = [m.upper() for m in ("get", "post", "put", "delete", "patch")
               if any(isinstance(s, ast.FunctionDef) and s.name == m
                      for s in ast.walk(cls))]
    fdef = next((s for s in cls.body if isinstance(s, ast.FunctionDef)
                 and s.name in ("get", "post")), None)
    return (methods or ["GET"], fdef, name)


def _view_ref(node, funcs, classes):
    """view 参数 → (methods, FunctionDef|None, 显示名);解析不了返回 None。"""
    if isinstance(node, ast.Name):                     # 直接函数名(同模块 import)
        f = next((f for (m, n), f in funcs.items() if n == node.id), None)
        return (["GET"], f, node.id) if f else None    # type: ignore[list-item]
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        key = (node.value.id, node.attr)               # views.detail
        if key in funcs:
            return ["GET"], funcs[key], node.attr      # type: ignore[list-item]
        if key in classes:
            return _class_view(classes[key], node.attr)
        f = next((f for (m, n), f in funcs.items() if n == node.attr), None)
        return (["GET"], f, node.attr) if f else None  # type: ignore[list-item]
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
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "urlpatterns" for t in node.targets
        ) and isinstance(node.value, (ast.List, ast.Tuple)):
            return list(node.value.elts)
    return []


def extract_django(app_root: Path) -> AppModel:
    model = AppModel(app_root=str(app_root), framework="django")
    ex = EndpointExtractor(str(app_root), model.config_flags)
    funcs, classes = _load_defs(app_root)

    urlconfs: dict[Path, ast.Module] = {}
    for p in sorted(app_root.rglob("urls.py")):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        try:
            urlconfs[p] = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        except SyntaxError:
            continue
    by_module = {".".join(p.with_suffix("").parts[len(app_root.parts):]): t
                 for p, t in urlconfs.items()}
    by_short = {p.parent.name: t for p, t in urlconfs.items()}

    def walk(els: list, prefix: str, origin: Path):
        for elt in els:
            if not (isinstance(elt, ast.Call)
                    and isinstance(elt.func, (ast.Name, ast.Attribute))):
                continue
            fname = (elt.func.id if isinstance(elt.func, ast.Name)
                     else elt.func.attr)
            if fname not in ("path", "re_path"):
                continue
            route = _literal(elt.args[0]) if elt.args else ""
            view = elt.args[1] if len(elt.args) > 1 else None
            full = prefix + (route or "")
            if isinstance(view, ast.Call) and isinstance(view.func, ast.Name) \
                    and view.func.id == "include":
                target = _literal(view.args[0]) if view.args else ""
                sub = None
                if target:
                    sub = (by_module.get(target)
                           or by_module.get(target.rstrip("/") + ".urls")
                           or by_module.get(target + ".urls")
                           or by_short.get(target.split(".")[0].rstrip("/")))
                if sub is not None:
                    walk(_parse_urlpatterns(sub), full, origin)
                continue                                  # 第三方 include 静默跳过
            if view is None:
                continue
            ref = _view_ref(view, funcs, classes)
            if ref is None:
                continue                                  # admin.site.urls 等
            methods, fdef, name = ref
            ep = Endpoint(func=name, route=full, methods=methods,
                          params=_path_params(full), file=str(origin),
                          line=(fdef.lineno if fdef else 0))
            if fdef is not None:
                ex._extract_endpoint_into(fdef, full, methods, ep)
            model.endpoints.append(ep)

    for p, tree in urlconfs.items():
        walk(_parse_urlpatterns(tree), "", p)
    model.candidates.extend(ex.candidates)
    model.config_flags.update(ex.config_flags)
    return model
