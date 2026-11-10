"""状态面图构建测试:转移点→produces,读位点→reads,join 与噪声门。"""

from __future__ import annotations

import textwrap
from pathlib import Path

from stateforge.state.sites import extract_sites
from stateforge.state.graph import build_state_plane_graph

SAMPLE = textwrap.dedent("""
    def login(request):
        request.session["id"] = 1
        request.session.permanent = True

    def logout(request):
        request.session.flush()

    def admin_page(request):
        if not request.session.get("id"):
            return None
        user = request.session["id"]
        return user

    def plain_row(row):
        return row["x"]          # 无同链写证据,不得建 slot
""")


def _graph(tmp_path: Path):
    f = tmp_path / "app.py"
    f.write_text(SAMPLE, encoding="utf-8")
    scan = extract_sites([tmp_path])
    return build_state_plane_graph(scan)


def test_slots_from_transition_points(tmp_path):
    g = _graph(tmp_path)
    assert 'request.session[id]' in g.slots
    assert 'request.session[permanent]' in g.slots
    assert 'request.session[*]' in g.slots          # flush → 整库


def test_produces_edges(tmp_path):
    g = _graph(tmp_path)
    ops = {(p.action, p.slot, p.op) for p in g.produces}
    assert ("login", "request.session[id]", "write") in ops
    assert ("login", "request.session[permanent]", "write") in ops
    assert ("logout", "request.session[*]", "flush") in ops


def test_reads_join_and_guard_flag(tmp_path):
    g = _graph(tmp_path)
    pairs = {(r.action, r.slot) for r in g.reads}
    assert ("admin_page", "request.session[id]") in pairs
    flagged = [r for r in g.reads
               if r.action == "admin_page" and r.slot == "request.session[id]"]
    assert any(r.guard for r in flagged)            # 同函数有提前退出守卫


def test_read_without_chain_evidence_not_a_slot(tmp_path):
    g = _graph(tmp_path)
    assert not [s for s in g.slots.values() if s.chain == "row"]
    assert "plain_row" not in g.actions


def test_readonly_slot_marked(tmp_path):
    """同链有写证据、但该 key 只读 → 建 slot 且 written=False。"""
    src = textwrap.dedent("""
        def w(request):
            request.session["a"] = 1

        def r(request):
            if not request.session.get("b"):
                return None
            return request.session["b"]
    """)
    d = tmp_path / "ro.py"
    d.write_text(src, encoding="utf-8")
    scan = extract_sites([tmp_path])
    g = build_state_plane_graph(scan)
    assert g.slots["request.session[b]"].written is False


def test_dot_renders(tmp_path):
    g = _graph(tmp_path)
    dot = g.to_dot("t")
    assert "login" in dot and 'shape=ellipse' in dot
    assert 'style=dashed' in dot and "flush" in dot
