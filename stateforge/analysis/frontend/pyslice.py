"""Python dependency slice: which installed modules a program can reach.

``build_icfg`` reads the application tree and excludes virtualenvs.  That is
right for coverage of the application's own code, but it hides the definitions
the application *calls into*: a Django URLconf resolves to
``wagtail.admin.views.workflows`` or ``django.contrib.auth.views``, and those
live in ``site-packages``.  Their calls are outside the IR, so route
attribution and the state plane cannot follow a request past the view.

This module answers the narrower question an interprocedural CFG needs:
**which installed modules can this program actually reach?**

Reachability, not enumeration.  The slice is the transitive import closure of
the application modules inside the virtualenvs found under the app root, plus
any installed module named by a dotted string literal — ``include("
wagtail.urls")``, ``INSTALLED_APPS = ["wagtail.admin"]``, a settings hook.  A
module enters the slice because some source names it; nothing here knows the
name of any framework, and there is no table of "interesting" packages.

Ordering follows ``sys.path``: an application module shadows an installed
module of the same name, so the application version wins in the slice too.
"""

from __future__ import annotations

import ast
import os
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

# Directories that never contain importable source worth slicing.  ``tests``
# is excluded for the same reason the app scan excludes it: test modules are
# not part of the program that serves a request.
_SKIP_DIRS = {"__pycache__", "tests", "test", "migrations", "node_modules",
              ".git", ".tox", ".eggs", "build", "dist"}

_DOTTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


@dataclass(frozen=True)
class Slice:
    """A reachable installed-module set and the roots it came from."""

    modules: dict[str, Path]
    roots: tuple[Path, ...]
    truncated: bool = False

    def __len__(self) -> int:
        return len(self.modules)


# --------------------------------------------------------------- discovery


def _is_site_packages(path: Path) -> bool:
    return path.name == "site-packages" and path.is_dir()


def _site_packages_of(venv: Path) -> Iterator[Path]:
    """``site-packages`` inside a virtualenv, POSIX or Windows layout."""
    for candidate in sorted(venv.glob("lib/python*/site-packages")):
        yield candidate
    windows = venv / "Lib" / "site-packages"
    if windows.is_dir():
        yield windows
    # A directory that *is* a site-packages — a bare one, or a virtualenv
    # created with ``--system-site-packages`` — is used as-is.  The base
    # interpreter's own directory is never consulted: it is not part of this
    # application.
    if _is_site_packages(venv):
        yield venv


def _explicit_site_packages(path: Path) -> Iterator[Path]:
    """Interpret a caller-supplied root as a place installed modules live.

    Accepts a virtualenv, a ``site-packages`` directory, or any directory of
    importable modules assembled by hand (a vendored slice, a target's
    dependencies).  Guessing only between those shapes keeps the option
    useful without ever scanning the host interpreter.
    """
    found = list(_site_packages_of(path))
    if found:
        yield from found
        return
    if path.is_dir():
        yield path


def venv_site_packages(app_root: Path,
                       extra: Iterable[Path | str] = ()) -> list[Path]:
    """Every ``site-packages`` directory belonging to *app_root*'s virtualenvs.

    A virtualenv is identified by ``pyvenv.cfg`` — the file ``venv`` and
    ``virtualenv`` both write — not by a hardcoded directory name, so
    ``.venv``, ``venv``, ``env`` and a custom location all qualify.  A bare
    directory named ``site-packages`` is accepted too: that name is the
    convention the import system itself uses for installed code.

    *extra* lets a caller name roots explicitly (a venv kept outside the app
    root, or a hand-assembled framework slice); either a venv directory or a
    ``site-packages`` directory is accepted.
    """
    found: list[Path] = []
    seen: set[Path] = set()

    def add(sites: Iterable[Path]) -> None:
        for site in sites:
            resolved = site.resolve()
            if resolved not in seen:
                seen.add(resolved)
                found.append(resolved)

    for venv in _find_virtualenvs(app_root):
        add(_site_packages_of(venv))
    for raw in extra:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = app_root / path
        add(_explicit_site_packages(path.resolve()))
    return found


def _find_virtualenvs(root: Path, max_depth: int = 4) -> Iterator[Path]:
    """Virtualenv roots under *root*, without descending into one found.

    ``os.walk`` with pruning keeps this cheap on the multi-hundred-megabyte
    application trees this runs over: once a directory holds ``pyvenv.cfg``
    its internals are irrelevant, and dependency trees are never scanned
    twice.  A directory named ``site-packages`` is yielded as its own root
    for the same reason.
    """
    root = root.resolve()
    base_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        if "pyvenv.cfg" in filenames or _is_site_packages(current):
            yield current
            dirnames[:] = []
            continue
        if len(current.parts) - base_depth >= max_depth:
            dirnames[:] = []
            continue
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)


# ------------------------------------------------------------------- index


def module_index(site_packages: Path) -> dict[str, Path]:
    """Dotted module name -> file, for one ``site-packages`` directory.

    Namespace packages (directories without ``__init__.py``) are indexed the
    same way; the name is what an import statement would spell.
    """
    index: dict[str, Path] = {}
    for path in sorted(site_packages.rglob("*.py")):
        rel = path.relative_to(site_packages)
        if _SKIP_DIRS & set(rel.parts[:-1]):
            continue
        parts = list(rel.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts:
            continue
        index.setdefault(".".join(parts), path)
    return index


# ------------------------------------------------------------------- seeds


def module_seeds(tree: ast.Module, module: str) -> Iterator[str]:
    """Module names *tree* names, spelled as an import would spell them.

    Three sources, all language-level: ``import`` statements, ``from``
    statements (including relative ones), and dotted string literals.  The
    literal case is what makes URLconf includes and settings references
    visible — the code names the module as data rather than importing it.
    """
    package = module.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
                parts = alias.name.split(".")
                for cut in range(len(parts) - 1, 0, -1):
                    yield ".".join(parts[:cut])
        elif isinstance(node, ast.ImportFrom):
            base = _absolute(node.module, node.level or 0, package)
            if base:
                yield base
            for alias in node.names:
                if alias.name != "*":
                    yield f"{base}.{alias.name}" if base else alias.name
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            if _DOTTED.match(text):
                yield text


def _absolute(module: str | None, level: int, package: str) -> str:
    """Absolute module name for a ``from`` statement (mirrors the runtime)."""
    if level <= 0:
        return module or ""
    parts = package.split(".") if package else []
    drop = level - 1
    if drop > len(parts):
        return ""
    base = parts[: len(parts) - drop]
    if module:
        base = base + module.split(".")
    return ".".join(p for p in base if p)


# ------------------------------------------------------------------ slicing


def slice_modules(app_root: Path,
                  app_sources: dict[str, Path],
                  extra_roots: Iterable[Path | str] = (),
                  max_files: int | None = None) -> Slice:
    """Transitive import closure of *app_sources* restricted to installed code.

    *app_sources* maps the dotted name of every application module to its
    file.  Those modules seed the walk — their imports and dotted literals are
    what name installed code — and they shadow installed modules of the same
    name, matching ``sys.path`` order.  Only modules that resolve inside a
    ``site-packages`` directory are returned; the application is scanned by
    its own frontend already.

    *max_files* bounds the walk.  A library that re-exports its whole API from
    ``__init__`` can make the closure grow; the cap keeps a single build
    finite, and ``Slice.truncated`` reports that it fired.
    """
    roots = venv_site_packages(app_root, extra_roots)
    installed: dict[str, Path] = {}
    for root in roots:
        for name, path in module_index(root).items():
            installed.setdefault(name, path)

    queue: deque[str] = deque()
    visited: set[str] = set()
    for name, path in sorted(app_sources.items()):
        visited.add(name)                  # application version wins
        tree = _parse(path)
        if tree is not None:
            queue.extend(module_seeds(tree, name))

    picked: dict[str, Path] = {}
    while queue:
        name = queue.popleft()
        if not name or name in visited:
            continue
        visited.add(name)
        path = installed.get(name)
        if path is None:
            continue                       # absent, stdlib or application
        if max_files is not None and len(picked) >= max_files:
            return Slice(modules=picked, roots=tuple(roots), truncated=True)
        picked[name] = path
        tree = _parse(path)
        if tree is None:
            continue
        queue.extend(module_seeds(tree, name))
    return Slice(modules=picked, roots=tuple(roots))


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None
