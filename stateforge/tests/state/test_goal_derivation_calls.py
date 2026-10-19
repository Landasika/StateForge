"""test_goal_derivation_calls.py —— PR-01:含调用函数的必要守卫分析正确性。

缺陷背景(实测,修复前):ICFG 把 callsite→returnsite 连接拆成过程间
call/return 边(callee 块在函数块集外),`_function_blocks` 过滤后函数
子图在每个调用点断裂 —— vulnshop `ship_order` 上 entry 对 UPDATE 目标
不可达 → 任一边移除都"不可达" → 全部分支误报必要且方向恒 true。

修复:函数内投影 CFG(`_intra_adjacency`)为可正常返回的调用点补
隐式 callsite→returnsite 边;全路径 raise 的被调者不连(不可返回边界);
基础图上目标不可达 → necessary_guard_edges 诚实返回空。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stateforge.analysis.static.icfg import build_icfg
from stateforge.state.goal_derivation import (
    guard_analysis_diagnostic,
    necessary_guard_edges,
)

VULNSHOP = Path(__file__).parents[2] / "targets" / "vulnshop"


@pytest.fixture(scope="module")
def vulnshop_ir():
    return build_icfg(VULNSHOP)


def _find_block(ir, function: str, needle: str, in_file: str = "") -> str:
    for b in ir.blocks.values():
        if b.function != function:
            continue
        if in_file and in_file not in b.id:
            continue
        if any(needle in s for s in b.statements):
            return b.id
    raise AssertionError(f"未找到含 {needle!r} 的 {function} 块")


# ------------------------------------------------- 真实 vulnshop(缺陷靶)

def test_ship_order_only_true_guards_necessary(vulnshop_ir):
    """修复后:只有 err/order-is-None 是无条件必要(guards/owner/status
    各有 guards-false 旁路,非无条件)——修复前全部 5 个被误报。"""
    target = _find_block(vulnshop_ir, "ship_order",
                         "UPDATE orders SET status='SHIPPED'", in_file="app.py")
    edges = necessary_guard_edges(vulnshop_ir, target)
    got = {(vulnshop_ir.blocks[b].start_line, kind) for b, kind in edges}
    assert got == {(265, "false"), (268, "false")}


def test_target_reachable_through_callsites(vulnshop_ir):
    """基础投影图上 entry→UPDATE 可达(修复前不可达)。"""
    from stateforge.state.goal_derivation import (_entry_of,
                                                   _function_blocks,
                                                   _reachable)
    target = _find_block(vulnshop_ir, "ship_order",
                         "UPDATE orders SET status='SHIPPED'", in_file="app.py")
    fb = _function_blocks(vulnshop_ir, target)
    entry = _entry_of(vulnshop_ir, fb)
    assert entry is not None
    assert _reachable(entry, target, fb, vulnshop_ir)


def test_owner_status_necessary_under_guards_only(vulnshop_ir):
    """guards=1 语义由关系层承担:owner/status 关系带 config 路径条件,
    不在无条件支配集(两分析互补,由诊断暴露关系)。"""
    target = _find_block(vulnshop_ir, "ship_order",
                         "UPDATE orders SET status='SHIPPED'", in_file="app.py")
    diag = guard_analysis_diagnostic(vulnshop_ir, target)
    dom_lines = {g["line"] for g in diag["dominator_guards"]}
    assert {271, 273}.isdisjoint(dom_lines)          # 非无条件
    assert diag["relational_relation_lines"] == [268, 271, 273]
    assert any("line 271" in n and "conditional" in n for n in diag["notes"])
    assert any("line 273" in n and "conditional" in n for n in diag["notes"])


# ------------------------------------------------- 调用点边界夹具

_APP_TMPL = '''\
import sqlite3
_DB = sqlite3.connect(":memory:")

def _load(oid):
    return _DB.execute("SELECT * FROM rows WHERE id=?", (oid,)).fetchone()

def boom(oid):
    raise ValueError("always")

def quiet(oid):
    _DB.execute("INSERT INTO log VALUES (?)", (oid,))   # fall-off-end

def handler(oid):
    row = {callee}(oid)
    if row is None:
        return "nf", 404
    if row["owner"] != "admin":
        return "forbidden", 403
    _DB.execute("UPDATE rows SET done=1 WHERE id=?", (oid,))
    return "ok"
'''


def _build(tmp_path: Path, callee: str):
    d = tmp_path / callee
    d.mkdir()
    (d / "app.py").write_text(_APP_TMPL.format(callee=callee),
                              encoding="utf-8")
    return build_icfg(d)


def test_normal_return_callee_keeps_guard_necessary(tmp_path):
    """正常返回的调用:守卫边仍被正确判定必要(路径经隐式边连通)。"""
    ir = _build(tmp_path, "_load")
    target = _find_block(ir, "handler", "UPDATE rows SET done=1")
    got = {(ir.blocks[b].start_line, kind)
           for b, kind in necessary_guard_edges(ir, target)}
    # 两守卫都在唯一路径上(无 wrapper 旁路)→ 双双无条件必要,方向 false
    assert got == {(15, "false"), (17, "false")}


def test_never_return_callee_makes_target_unreachable(tmp_path):
    """全路径 raise 的被调者:调用后代码不可达 → 诚实返回空,不误报。"""
    ir = _build(tmp_path, "boom")
    target = _find_block(ir, "handler", "UPDATE rows SET done=1")
    assert necessary_guard_edges(ir, target) == []


def test_fall_off_end_callee_returns_none(tmp_path):
    """无 return 语句的被调者:隐式返回 None → 连接保留,守卫必要。"""
    ir = _build(tmp_path, "quiet")
    target = _find_block(ir, "handler", "UPDATE rows SET done=1")
    got = {(ir.blocks[b].start_line, kind)
           for b, kind in necessary_guard_edges(ir, target)}
    assert got == {(15, "false"), (17, "false")}


def test_unresolved_external_call_unchanged(tmp_path):
    """外部调用(jsonify/外部导入)不产 callsite 块:无回归(路径天然连通)。"""
    src = '''\
import sqlite3
_DB = sqlite3.connect(":memory:")

def _load(oid):
    return _DB.execute("SELECT * FROM rows WHERE id=?", (oid,)).fetchone()

def handler(oid):
    row = _load(oid)
    if row is None:
        return jsonify("nf"), 404
    _DB.execute("UPDATE rows SET done=1 WHERE id=?", (oid,))
    return jsonify("ok")
'''
    d = tmp_path / "ext"
    d.mkdir()
    (d / "app.py").write_text(src, encoding="utf-8")
    ir = build_icfg(d)
    target = _find_block(ir, "handler", "UPDATE rows SET done=1")
    got = {(ir.blocks[b].start_line, kind)
           for b, kind in necessary_guard_edges(ir, target)}
    assert got == {(9, "false")}      # jsonify 无守卫;仅 row-is-None(L9)必要


def test_guard_between_two_calls(tmp_path):
    """守卫前后各有一个调用:两侧隐式边都参与路径,守卫判定不受影响。"""
    src = '''\
import sqlite3
_DB = sqlite3.connect(":memory:")

def _load(oid):
    return _DB.execute("SELECT * FROM rows WHERE id=?", (oid,)).fetchone()

def _touch(oid):
    _DB.execute("INSERT INTO audit VALUES (?)", (oid,))
    return oid

def handler(oid):
    row = _load(oid)
    if row is None:
        return "nf", 404
    _DB.execute("UPDATE rows SET done=1 WHERE id=?", (oid,))
    _touch(oid)
    return "ok"
'''
    d = tmp_path / "two"
    d.mkdir()
    (d / "app.py").write_text(src, encoding="utf-8")
    ir = build_icfg(d)
    target = _find_block(ir, "handler", "UPDATE rows SET done=1")
    got = {(ir.blocks[b].start_line, kind)
           for b, kind in necessary_guard_edges(ir, target)}
    assert got == {(13, "false")}     # 目标前仅 row-is-None 守卫;_touch 在目标后
