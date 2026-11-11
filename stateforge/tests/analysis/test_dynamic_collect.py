"""Tests for driving a target's own harness under a trace.

The collector exists to close one specific gap: a static graph has call sites
it cannot resolve, and a trace shows where they actually went.  These tests pin
the three things that make that work, because each one fails silently.

* The harness may ``chdir``; a relative analysis root then means a different
  tree and the graph comes out empty rather than wrong.
* An in-tree virtualenv sits *inside* the analysis root, so a plain prefix
  filter records installed code and the learned map fills with frames the graph
  has never heard of.
* An event only teaches anything if it names a function the IR contains.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from stateforge.analysis.dynamic import collect
from stateforge.fuzzing import dynamic_collect
from stateforge.runtime.tracer import TraceEvent


def _event(path: Path, function: str, line: int = 1,
           kind: str = "line") -> TraceEvent:
    return TraceEvent(0, 0, str(path), function, line, kind)


@pytest.fixture
def app(tmp_path):
    """An application tree that also holds a virtualenv of its own."""
    root = tmp_path / "app"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "views.py").write_text("def index():\n    return 1\n")
    venv = root / ".venv"
    site = venv / "lib" / "python3.13" / "site-packages"
    (site / "jinja2").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (site / "jinja2" / "nodes.py").write_text("def iter_fields():\n    pass\n")
    return root


def test_project_events_drops_files_outside_the_root(app, tmp_path):
    inside = app / "pkg" / "views.py"
    outside = tmp_path / "elsewhere.py"
    events = [_event(inside, "index"), _event(outside, "other")]
    kept = collect.project_events(events, app)
    assert [e.function for e in kept] == ["index"]


def test_project_events_drops_an_in_tree_virtualenv(app):
    """The failure that made a 1.4M-event trace teach almost nothing."""
    project = _event(app / "pkg" / "views.py", "index")
    installed = _event(
        app / ".venv" / "lib" / "python3.13" / "site-packages"
        / "jinja2" / "nodes.py", "iter_fields")
    kept = collect.project_events([project, installed], app,
                                  collect.app_exclusions(app))
    assert [e.function for e in kept] == ["index"]


def test_app_exclusions_finds_the_venv_by_pyvenv_cfg(app):
    found = collect.app_exclusions(app)
    assert (app / ".venv").resolve() in found
    assert not collect.app_exclusions(app / "pkg")


def test_app_exclusions_respects_a_custom_venv_name(tmp_path):
    """Discovery is by marker file, not by the name ``.venv``."""
    root = tmp_path / "proj"
    (root / "env" / "lib" / "python3.12" / "site-packages").mkdir(parents=True)
    (root / "env" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    assert (root / "env").resolve() in collect.app_exclusions(root)


def test_restored_cwd_puts_the_directory_back(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(tmp_path)
    with dynamic_collect._restored_cwd():
        os.chdir(elsewhere)
        assert os.getcwd() == os.path.realpath(elsewhere)
    assert os.getcwd() == os.path.realpath(tmp_path)


def test_restored_cwd_restores_even_when_drive_raises(tmp_path, monkeypatch):
    start = os.getcwd()
    with pytest.raises(RuntimeError):
        with dynamic_collect._restored_cwd():
            os.chdir(tmp_path)
            raise RuntimeError("harness exploded")
    assert os.getcwd() == os.path.realpath(start)


def test_collect_events_keeps_events_and_reports_failure(tmp_path):
    target = tmp_path / "mod.py"

    def drive():
        raise ValueError("no")

    result = dynamic_collect.collect_events(drive, tmp_path, label="demo")
    assert result.label == "demo"
    assert result.error and "ValueError" in result.error
    assert result.event_count == 0


def test_collect_events_records_requests_returned_by_the_drive(tmp_path):
    result = dynamic_collect.collect_events(lambda: [("/a", 200), ("/b", 404)],
                                    tmp_path, label="demo")
    assert result.requests == [("/a", 200), ("/b", 404)]


def test_uncovered_sites_lists_only_what_the_trace_missed():
    class FakeIR:
        metadata = {"unresolved_calls": [
            {"caller": "a.run", "line": 3, "callee": "first"},
            {"caller": "a.run", "line": 4, "callee": "second"},
        ]}

    left = collect.uncovered_sites(FakeIR(), {("a.run", 3): "a.first"})
    assert [row["line"] for row in left] == [4]


def test_interpreter_for_finds_the_venv_python(app):
    python = collect.interpreter_for(app)
    # The fixture writes no interpreter; discovery must not invent one.
    assert python is None or python.endswith("python")


def test_interpreter_for_reports_the_binary_when_present(tmp_path):
    root = tmp_path / "proj"
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    binary = root / ".venv" / "bin" / "python"
    binary.write_text("#!/bin/sh\n")
    assert collect.interpreter_for(root) == str(binary.resolve())


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_path_translation_maps_an_installed_copy_onto_the_source_tree(tmp_path):
    """``pip install .`` copies the package; the graph reads the source tree.

    fava, OctoPrint and Radicale all run from ``site-packages`` while the IR is
    built from ``src/``.  Without translation the trace and the graph share no
    file names and the target learns nothing at all.
    """
    root = tmp_path / "proj"
    source = _write(root / "src" / "pkg" / "views.py", "def index():\n    return 1\n")
    site = root / ".venv" / "lib" / "python3.13" / "site-packages"
    _write(root / ".venv" / "pyvenv.cfg", "home = /usr/bin\n")
    installed = _write(site / "pkg" / "views.py", "def index():\n    return 1\n")

    translation = collect.path_translation(root, [str(source)])
    assert translation[str(installed.resolve())] == str(source.resolve())


def test_path_translation_refuses_a_copy_that_differs_in_content(tmp_path):
    """trac's installed copy is not the analysed source; do not rewrite it."""
    root = tmp_path / "proj"
    source = _write(root / "src" / "pkg" / "views.py", "def index():\n    return 1\n")
    site = root / ".venv" / "lib" / "python3.13" / "site-packages"
    _write(root / ".venv" / "pyvenv.cfg", "home = /usr/bin\n")
    _write(site / "pkg" / "views.py", "def index():\n    return 2  # different\n")

    assert collect.path_translation(root, [str(source)]) == {}


def test_path_translation_ignores_files_outside_the_dependency_roots(tmp_path):
    root = tmp_path / "proj"
    source = _write(root / "src" / "pkg" / "views.py", "def index():\n    return 1\n")
    _write(root / "pkg" / "views.py", "def index():\n    return 1\n")
    assert collect.path_translation(root, [str(source)]) == {}


def test_translate_events_rewrites_the_file_and_keeps_the_rest(tmp_path):
    source = str(tmp_path / "src" / "pkg" / "views.py")
    installed = str(tmp_path / "site" / "pkg" / "views.py")
    event = _event(Path(installed), "index", line=7)
    moved = collect.translate_events([event], {installed: source})
    assert moved[0].file == source
    assert (moved[0].function, moved[0].line) == ("index", 7)


def test_translate_events_is_a_no_op_without_a_translation(tmp_path):
    event = _event(tmp_path / "a.py", "run")
    assert collect.translate_events([event], {})[0].file == str(tmp_path / "a.py")


def test_prepare_events_translates_then_filters(app):
    """Order matters: translate first, so a mapped file survives the venv cut."""
    installed = (app / ".venv" / "lib" / "python3.13" / "site-packages"
                 / "pkg" / "views.py")
    source = app / "pkg" / "views.py"

    class FakeIR:
        functions = {}

    translation = {str(installed.resolve()): str(source.resolve())}
    from unittest import mock
    with mock.patch.object(collect, "path_translation", return_value=translation):
        kept = collect.prepare_events([_event(installed, "index")], app, FakeIR())
    assert [e.file for e in kept] == [str(source.resolve())]
