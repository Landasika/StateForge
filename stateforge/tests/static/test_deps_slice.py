"""Dependency slice: which installed modules enter the ICFG.

The slice exists because route attribution could not match an endpoint whose
view lives in ``site-packages``: the URLconf resolves into the virtualenv, but
the ICFG only contained application code.  These tests pin the three
behaviours that make the feature safe:

* discovery is by ``pyvenv.cfg``, not by a directory name or a framework list;
* a module enters the slice because the application reaches it — through an
  import or a dotted string literal such as ``include("pkg.urls")``;
* the default build is unchanged, so existing coverage numbers still compare.
"""

from __future__ import annotations

from pathlib import Path

from stateforge.analysis.frontend.pyslice import (
    Slice,
    module_index,
    module_seeds,
    slice_modules,
    venv_site_packages,
)
from stateforge.analysis.static.icfg import build_icfg


def _py(p: Path, src: str = "") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src, encoding="utf-8")


def _venv(root: Path, name: str = ".venv") -> Path:
    """A minimal virtualenv: ``pyvenv.cfg`` plus a ``site-packages``."""
    venv = root / name
    _py(venv / "pyvenv.cfg", "home = /usr/bin\n")
    site = venv / "lib/python3.13/site-packages"
    site.mkdir(parents=True, exist_ok=True)
    return site


def _app(tmp: Path) -> tuple[Path, Path]:
    """An app that reaches one installed package by import and one by name."""
    root = tmp / "app"
    site = _venv(root)
    _py(root / "proj" / "urls.py", """
from django.conf.urls import include, path
import helpers.text

urlpatterns = [path("a/", helpers.text.view),
               path("b/", include("extpkg.urls"))]
""")
    _py(site / "helpers" / "__init__.py", "")
    _py(site / "helpers" / "text.py", "def view(request):\n    return 1\n")
    _py(site / "extpkg" / "__init__.py", "")
    _py(site / "extpkg" / "urls.py",
        "urlpatterns = []\n\n\ndef z(request):\n    return 2\n")
    _py(site / "unrelated" / "__init__.py", "def never(request):\n    return 3\n")
    return root, site


def test_venv_discovered_by_pyvenv_cfg_not_by_name(tmp_path):
    root = tmp_path / "app"
    custom = _venv(root, name="my-env")
    assert venv_site_packages(root) == [custom.resolve()]


def test_nested_and_system_site_packages(tmp_path):
    root = tmp_path / "app"
    (root / "svc").mkdir(parents=True)
    nested = _venv(root / "svc", name="env")
    bare = root / "site-packages"
    bare.mkdir()
    found = venv_site_packages(root)
    assert nested.resolve() in found and bare.resolve() in found


def test_module_index_spells_names_as_imports_do(tmp_path):
    site = _venv(tmp_path / "app")
    _py(site / "pkg" / "__init__.py", "")
    _py(site / "pkg" / "sub" / "__init__.py", "")
    _py(site / "pkg" / "sub" / "mod.py", "")
    index = module_index(site)
    assert index["pkg"] == site / "pkg" / "__init__.py"
    assert index["pkg.sub"] == site / "pkg" / "sub" / "__init__.py"
    assert index["pkg.sub.mod"] == site / "pkg" / "sub" / "mod.py"


def test_seeds_include_imports_froms_and_dotted_literals():
    import ast

    tree = ast.parse("""
import a.b
from c import d
from .sibling import e
x = ["lit.pkg.mod", "not dotted"]
""")
    seeds = set(module_seeds(tree, "pkg.here.mod"))
    assert "a.b" in seeds and "a" in seeds
    assert "c" in seeds and "c.d" in seeds
    assert "pkg.here.sibling" in seeds
    assert "lit.pkg.mod" in seeds


def test_slice_takes_reachable_modules_only(tmp_path):
    root, site = _app(tmp_path)
    app = {"proj.urls": root / "proj/urls.py"}
    sliced = slice_modules(root, app)
    assert "helpers.text" in sliced.modules
    assert "extpkg.urls" in sliced.modules        # reached via dotted literal
    assert "unrelated" not in sliced.modules      # never named
    assert sliced.roots == (site.resolve(),)
    assert sliced.truncated is False


def test_application_module_shadows_installed(tmp_path):
    root, site = _app(tmp_path)
    _py(root / "helpers.py", "own = 1\n")
    app = {"proj.urls": root / "proj/urls.py",
           "helpers": root / "helpers.py"}
    sliced = slice_modules(root, app)
    assert "helpers" not in sliced.modules


def test_max_files_reports_truncation(tmp_path):
    root, site = _app(tmp_path)
    app = {"proj.urls": root / "proj/urls.py"}
    sliced = slice_modules(root, app, max_files=1)
    assert len(sliced.modules) == 1
    assert sliced.truncated is True


def test_empty_slice_without_virtualenv(tmp_path):
    root = tmp_path / "app"
    _py(root / "mod.py", "import os\n")
    sliced = slice_modules(root, {"mod": root / "mod.py"})
    assert sliced == Slice(modules={}, roots=())


# ------------------------------------------------------------------- ICFG


def test_installed_views_enter_the_icfg_only_on_request(tmp_path):
    root, site = _app(tmp_path)
    plain = build_icfg(root)
    assert not any("site-packages" in b.file for b in plain.blocks.values())

    sliced = build_icfg(root, deps=True)
    files = {b.file for b in sliced.blocks.values()}
    assert str(site / "helpers/text.py") in files
    assert str(site / "extpkg/urls.py") in files
    assert str(site / "unrelated/__init__.py") not in files
    # ``helpers`` and ``helpers.text`` from the import, ``extpkg.urls`` from
    # the dotted literal.  A literal names exactly one module; its parent
    # package is reached only if some source imports it.
    assert sliced.metadata["deps"]["module_count"] == 3
    assert sliced.metadata["deps"]["truncated"] is False


def test_installed_block_ids_stay_relative_to_site_packages(tmp_path):
    root, _ = _app(tmp_path)
    ir = build_icfg(root, deps=True)
    ids = [b for b in ir.blocks if b.startswith("helpers/")]
    assert ids, "expected blocks from the installed module"
    assert all(b.startswith("helpers/text.py::") for b in ids)


def test_explicit_deps_root_is_honoured(tmp_path):
    vendored = tmp_path / "vendor-site"
    _py(vendored / "external" / "__init__.py", "def f():\n    return 1\n")
    root = tmp_path / "app"
    _py(root / "mod.py", "import external\n")
    ir = build_icfg(root, deps=[vendored])
    assert any(b.startswith("external/") for b in ir.blocks)


def test_source_hash_covers_slice_files(tmp_path):
    """A stale hash must not match once installed code changes."""
    from stateforge.analysis.frontend.python import _dependency_slice, _source_hash

    root, _ = _app(tmp_path)
    resolved = root.resolve()
    ir = build_icfg(resolved, deps=True)

    def current_hash() -> str:
        return _source_hash(resolved, _dependency_slice(resolved).modules.values())

    assert ir.metadata["source_hash"] == current_hash()
    touched = sorted(_dependency_slice(resolved).modules.values())[0]
    touched.write_text(touched.read_text() + "\n# touched\n", encoding="utf-8")
    assert ir.metadata["source_hash"] != current_hash()


def test_metadata_keys_unchanged_when_deps_off(tmp_path):
    root, _ = _app(tmp_path)
    ir = build_icfg(root)
    assert set(ir.metadata) == {
        "root", "kind", "version", "callgraph_supplied", "function_count",
        "block_count", "edge_count", "unresolved_calls", "source_hash"}
