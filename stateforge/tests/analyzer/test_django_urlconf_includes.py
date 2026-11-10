"""URLconf include 解析测试:包/子模块、变量 include、venv、i18n_patterns、老式 url()。

覆盖 2026-11-10 适配器修复(bakerydemo/zinnia 端点 0 的两个根因)。
"""

from __future__ import annotations

import ast
from pathlib import Path

from stateforge.analysis.django_adapter import (
    _ExtDefs, _iter_urlconf_files, _module_key, _parse_urlpatterns,
    _venv_module_file, extract_django)


def _py(p: Path, src: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src, encoding="utf-8")


def _make_app(tmp: Path, with_venv: bool = True) -> Path:
    root = tmp / "app"
    _py(root / "proj" / "urls.py", """
from django.urls import include, path
from vendor import urls as vendor_urls
from vendor import views

urlpatterns = [
    path("v/", include(vendor_urls)),
    path("e/", include("extpkg.urls")),
    path("old/", None_placeholder) if False else path("x/", views.x),
]
urlpatterns += [path("a/", views.a)]
urlpatterns += i18n_patterns(path("b/", views.b))
""".replace("path(\"old/\", None_placeholder) if False else ", ""))
    _py(root / "vendor" / "urls" / "__init__.py", """
from django.urls import include, path
from vendor import views

urlpatterns = [
    path("x/", views.x),
    path("sub/", include("vendor.urls.sub")),
]
""")
    _py(root / "vendor" / "urls" / "sub.py", """
from django.urls import path
from vendor import views

urlpatterns = [path("y/", views.y)]
""")
    _py(root / "vendor" / "views.py", """
def x(request):
    return 1

def y(request):
    return 2

def a(request):
    return 3

def b(request):
    return 4
""")
    if with_venv:
        sp = root / ".venv/lib/python3.13/site-packages"
        _py(sp / "extpkg" / "urls.py", """
from django.urls import path
from extpkg import views

urlpatterns = [path("z/", views.z)]
""")
        _py(sp / "extpkg" / "views.py", """
def z(request):
    return 5
""")
    return root


def _routes(root: Path) -> dict[str, str]:
    model = extract_django(root)
    return {ep.route: ep.func for ep in model.endpoints if ep.route}


def test_urls_package_and_submodule_included(tmp_path):
    routes = _routes(_make_app(tmp_path))
    assert routes.get("v/x/") == "x"          # urls 包(__init__.py)
    assert routes.get("v/sub/y/") == "y"      # 包的子模块


def test_variable_include_via_import_alias(tmp_path):
    routes = _routes(_make_app(tmp_path))
    assert routes.get("v/x/") == "x"          # include(vendor_urls)


def test_venv_include_resolves(tmp_path):
    root = _make_app(tmp_path)
    routes = _routes(root)
    assert routes.get("e/z/") == "z"          # include("extpkg.urls") → venv
    model = extract_django(root)
    ext_ep = [e for e in model.endpoints if e.route == "e/z/"]
    assert any("site-packages" in e.file for e in ext_ep)


def test_i18n_patterns_and_augmented_assign(tmp_path):
    routes = _routes(_make_app(tmp_path))
    assert routes.get("a/") == "a"            # urlpatterns += [...]
    assert routes.get("b/") == "b"            # urlpatterns += i18n_patterns(...)


def test_urlconf_file_iterator_covers_packages(tmp_path):
    root = _make_app(tmp_path)
    files = {str(p.relative_to(root)) for p in _iter_urlconf_files(root)}
    assert "vendor/urls/__init__.py" in files
    assert "vendor/urls/sub.py" in files
    assert "proj/urls.py" in files


def test_venv_module_file_dots_to_path(tmp_path):
    sp = tmp_path / "sp"
    _py(sp / "pkg" / "sub" / "__init__.py", "")
    _py(sp / "pkg" / "mod.py", "")
    assert _venv_module_file(sp, "pkg.sub") == sp / "pkg/sub/__init__.py"
    assert _venv_module_file(sp, "pkg.mod") == sp / "pkg/mod.py"
    assert _venv_module_file(sp, "pkg.missing") is None


def test_parse_urlpatterns_accumulates():
    tree = ast.parse("""
from django.conf.urls.i18n import i18n_patterns
from django.urls import path
urlpatterns = [path("a/", v.a)]
urlpatterns += [path("b/", v.b)]
urlpatterns += i18n_patterns(path("c/", v.c))
urlpatterns += static("/media/")
""")
    pats = _parse_urlpatterns(tree)
    routes = [p.args[0].value for p in pats]
    assert routes == ["a/", "b/", "c/"]       # static(...) 非列表,跳过
