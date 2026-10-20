"""SS-07E/F: pre-registration is frozen before results and never fabricates arms."""

from __future__ import annotations

import json
from pathlib import Path

from stateforge.evaluation.real_apps import pre_register, tree_hash


def test_pre_registration_contains_five_hashed_apps_before_results(tmp_path):
    manifest = pre_register(tmp_path)
    assert [a["name"] for a in manifest["apps"]] == [
        "CTFd", "healthchecks", "linkding", "Spirit", "microblog"]
    assert all(a["pre_registered"] for a in manifest["apps"])
    assert all(len(a["source_hash"]) == 64 for a in manifest["apps"])
    assert not (tmp_path / "results.json").exists()
    written = json.loads((tmp_path / "pre_registered.json").read_text())
    assert written["source_revision"] == manifest["source_revision"]


def test_tree_hash_is_content_sensitive_and_ignores_caches(tmp_path):
    d = tmp_path / "app"
    d.mkdir()
    (d / "a.py").write_text("x = 1\n")
    h1 = tree_hash(d)
    (d / "a.py").write_text("x = 2\n")
    h2 = tree_hash(d)
    assert h1 != h2
    (d / "__pycache__").mkdir()
    (d / "__pycache__" / "a.pyc").write_bytes(b"cache")
    assert tree_hash(d) == h2
