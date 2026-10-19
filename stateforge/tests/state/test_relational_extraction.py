"""test_relational_extraction.py —— 关系自动提取(阶段 2,验收关口 A)。

关口 A(任务书):
- 不给 ship_order() 编写专门关系模板,自动提取
  ``order.user_id == user.id`` 与 ``order.status == "PAID"``;
- guards 配置条件、反向分支(守卫真侧目标)、函数不可解析均正确处理;
- 泛化性:同一机制对改名实体(ticket/acct)提取改名关系——
  无任何硬编码业务名(评审约束:"不靠预先写好的业务规则演示成功")。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from stateforge.analysis.static.icfg import build_icfg
from stateforge.state.goal_derivation import derive_relational_goal
from stateforge.state.relational import Const, Field, RelOp, RelPredicate

VULNSHOP = Path(__file__).parents[2] / "targets" / "vulnshop"


@pytest.fixture(scope="module")
def vulnshop_ir():
    return build_icfg(VULNSHOP)


def _find_block(ir, function: str, needle: str, in_file: str = "") -> str:
    for b in ir.blocks.values():
        if b.function != function:
            continue
        if in_file and in_file not in b.id:
            continue          # 目录含 app_anota.py 同名函数,按文件消歧
        if any(needle in s for s in b.statements):
            return b.id
    raise AssertionError(f"未找到含 {needle!r} 的 {function} 块")


# ------------------------------------------------- 真实 vulnshop 关口 A

def test_ship_order_update_goal(vulnshop_ir):
    """UPDATE 目标:四条件自动提取,无手写归属规则。"""
    target = _find_block(vulnshop_ir, "ship_order", "UPDATE orders SET status='SHIPPED'",
                 in_file="app.py")
    goal = derive_relational_goal(vulnshop_ir, target)
    assert goal is not None

    rels = {r.render().split("@")[0]: r for r in goal.relations}
    # 条件 3:归属关系(Field-Field,NotEq 早退反转)
    assert "EQ(?order.user_id, ?user.id)" in rels
    owner = rels["EQ(?order.user_id, ?user.id)"]
    assert owner.origin_op == "NotEq" and owner.via_edge == "false"
    assert owner.source_line == 271
    assert (owner.left.var, owner.left.name) == ("?order", "user_id")
    assert (owner.right.var, owner.right.name) == ("?user", "id")
    # 条件 4:支付状态(Field-Const)
    assert "EQ(?order.status, 'PAID')" in rels
    paid = rels["EQ(?order.status, 'PAID')"]
    assert paid.origin_op == "NotEq" and paid.source_line == 273
    assert paid.right == Const("PAID")
    # 条件 1:订单存在(Is None 早退反转)
    assert "EXISTS(?order)" in rels
    assert rels["EXISTS(?order)"].origin_op == "Is"


def test_ship_order_path_conditions(vulnshop_ir):
    """guards 配置旗标 → 路径条件(关系仅在 guards=1 时是前置)。"""
    target = _find_block(vulnshop_ir, "ship_order", "UPDATE orders SET status='SHIPPED'",
                 in_file="app.py")
    goal = derive_relational_goal(vulnshop_ir, target)
    pcs = {(p.var, p.requires, p.kind) for p in goal.path_conditions}
    assert ("guards", True, "config") in pcs     # 配置条件,不是状态谓词
    assert ("err", False, "local") in pcs        # 认证成功的局部分支口径
    # guards 不进关系(非实体变量)
    assert all(r.left.var != "?guards" for r in goal.relations)


def test_ship_order_provenance(vulnshop_ir):
    """变量来源链:route 参数→查询函数→行实体;token→认证主体。"""
    target = _find_block(vulnshop_ir, "ship_order", "UPDATE orders SET status='SHIPPED'",
                 in_file="app.py")
    goal = derive_relational_goal(vulnshop_ir, target)
    prov = {p.var: p for p in goal.var_provenance}
    assert set(prov) == {"?order", "?user"}

    o = prov["?order"]
    assert o.table == "orders" and o.key_param == "oid"
    assert any(s.startswith("call:get_order_or_none(") for s in o.origin)
    assert any("SELECT" in s and "orders" in s for s in o.origin)
    assert not o.auth_subject

    u = prov["?user"]
    assert u.table == "users" and u.auth_subject
    assert any("require_user" in s for s in u.origin)

    # oid 是路由参数(<int:oid>):var_kind 判定经 provenance 索引
    from stateforge.state.provenance import analyze_provenance
    idx = analyze_provenance(goal.file)
    assert idx.var_kind("ship_order", "oid") == "route-param"
    assert idx.var_kind("ship_order", "guards") == "config"


def test_reverse_branch_target_keeps_not_equal(vulnshop_ir):
    """反向分支:目标在守卫真侧(403 早退块)→ 谓词不反转(NE,via true)。"""
    target = _find_block(vulnshop_ir, "ship_order", "not your order", in_file="app.py")
    goal = derive_relational_goal(vulnshop_ir, target)
    rels = {r.render().split("@")[0]: r for r in goal.relations}
    ne = rels["NE(?order.user_id, ?user.id)"]
    assert ne.via_edge == "true" and ne.origin_op == "NotEq"
    # 403 块在 owner 守卫内、status 守卫外 → 无 status 关系
    assert not any("status" in r.render() for r in goal.relations)
    # 目标在 if guards: body 内 → guards 真值仍是路径条件
    pcs = {(p.var, p.requires) for p in goal.path_conditions}
    assert ("guards", True) in pcs


def test_goal_serializes_with_anchors(vulnshop_ir):
    """整目标可序列化:关系/路径条件/来源/不可解析项全部往返。"""
    target = _find_block(vulnshop_ir, "ship_order", "UPDATE orders SET status='SHIPPED'",
                 in_file="app.py")
    goal = derive_relational_goal(vulnshop_ir, target)
    d = goal.to_dict()
    assert len(d["relations"]) == 3
    assert RelPredicate.from_dict(d["relations"][0]) == goal.relations[0]
    assert d["unresolvable"] == []


# ------------------------------------------------- 泛化:改名实体夹具

_TICKET_APP = '''
import sqlite3, hmac
from flask import Flask, request, jsonify

_DBX = sqlite3.connect(":memory:")

def _load_ticket(tid):
    return _DBX.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()

def _current_account():
    tok = request.headers.get("Authorization", "")
    return _DBX.execute("SELECT * FROM accounts WHERE name=?", (tok,)).fetchone()

def _require_account():
    acct = _current_account()
    if acct is None:
        return None, (jsonify({"error": "unauthorized"}), 401)
    return acct, None

app = Flask(__name__)

@app.post("/tickets/<int:tid>/close")
def close_ticket(tid):
    acct, err = _require_account()
    if err:
        return err
    tkt = _load_ticket(tid)
    if tkt is None:
        return "nf", 404
    if tkt["owner"] != acct["uid"]:
        return "forbidden", 403
    if tkt["state"] != "OPEN":
        return "conflict", 409
    _DBX.execute("UPDATE tickets SET state='CLOSED' WHERE id=?", (tid,))
    return "ok"
'''


@pytest.fixture()
def ticket_dir(tmp_path):
    (tmp_path / "app.py").write_text(_TICKET_APP, encoding="utf-8")
    return tmp_path


def test_renamed_entities_extract_renamed_relations(ticket_dir):
    """零业务名词表:ticket/acct/owner/uid/OPEN 同机制提取(泛化性)。"""
    ir = build_icfg(ticket_dir)
    target = _find_block(ir, "close_ticket", "UPDATE tickets SET state='CLOSED'")
    goal = derive_relational_goal(ir, target)
    rels = {r.render().split("@")[0] for r in goal.relations}
    assert "EQ(?tkt.owner, ?acct.uid)" in rels
    assert "EQ(?tkt.state, 'OPEN')" in rels
    assert "EXISTS(?tkt)" in rels
    prov = {p.var: p for p in goal.var_provenance}
    assert prov["?tkt"].table == "tickets"
    assert prov["?acct"].table == "accounts" and prov["?acct"].auth_subject


# ------------------------------------------------- guards 变体与不可解析

_HEAD = ("import sqlite3\n_DB = sqlite3.connect(':memory:')\n\n\n"
         "def _load_row(rid):\n"
         '    return _DB.execute("SELECT * FROM rows WHERE id=?", (rid,)).fetchone()\n\n\n')

_WITH_WRAPPER = _HEAD + (
    "def handler(rid):\n"
    "    row = _load_row(rid)\n"
    "    if row is None:\n"
    '        return "nf", 404\n'
    "    if STRICT:\n"
    '        if row["owner"] != "admin":\n'
    '            return "forbidden", 403\n'
    '    _DB.execute("UPDATE rows SET done=1 WHERE id=?", (rid,))\n'
    '    return "ok"\n')

_WITHOUT_WRAPPER = _HEAD + (
    "def handler(rid):\n"
    "    row = _load_row(rid)\n"
    "    if row is None:\n"
    '        return "nf", 404\n'
    '    if row["owner"] != "admin":\n'
    '        return "forbidden", 403\n'
    '    _DB.execute("UPDATE rows SET done=1 WHERE id=?", (rid,))\n'
    '    return "ok"\n')


@pytest.mark.parametrize("src,expect_strict", [
    (_WITH_WRAPPER, True),
    (_WITHOUT_WRAPPER, False),
])
def test_config_wrapper_conditionality(tmp_path, src, expect_strict):
    """wrapper 在 → 关系带 config 路径条件;wrapper 无 → 关系无条件。"""
    d = tmp_path / ("w" if expect_strict else "wo")
    d.mkdir()
    (d / "app.py").write_text(src, encoding="utf-8")
    ir = build_icfg(d)
    target = _find_block(ir, "handler", "UPDATE rows SET done=1")
    goal = derive_relational_goal(ir, target)
    rels = {r.render().split("@")[0] for r in goal.relations}
    assert rels == {"EXISTS(?row)", 'EQ(?row.owner, \'admin\')'}
    pcs = {(p.var, p.requires, p.kind) for p in goal.path_conditions}
    if expect_strict:
        assert ("STRICT", True, "config") in pcs
    else:
        assert pcs == set()          # 无 wrapper → 无配置条件


def test_unresolvable_callee_yields_partial_not_guess(tmp_path):
    """callee 不可分析 → 关系照提,来源标 unresolved(不猜表)。"""
    src = '''
import sqlite3
_DB = sqlite3.connect(":memory:")

def handler(oid):
    row = load_from_external(oid)      # 本文件不可见:来源不可解析
    if row is None:
        return "nf", 404
    if row["owner"] != "admin":
        return "forbidden", 403
    _DB.execute("UPDATE rows SET done=1 WHERE id=?", (oid,))
    return "ok"
'''
    d = tmp_path / "unres"
    d.mkdir()
    (d / "app.py").write_text(src, encoding="utf-8")
    ir = build_icfg(d)
    target = _find_block(ir, "handler", "UPDATE rows SET done=1")
    goal = derive_relational_goal(ir, target)
    rels = {r.render().split("@")[0] for r in goal.relations}
    # 关系仍提取(比较本身可解析);?row 无来源 → unresolvable 保真
    assert 'EQ(?row.owner, \'admin\')' in rels
    assert "EXISTS(?row)" in rels
    assert any(u.what == "var:?row" and "provenance-unresolved" in u.cause
               for u in goal.unresolvable)
    assert goal.entity_vars == ("?row",)
    assert goal.var_provenance == ()          # 不虚构表


def test_boolop_guard_recorded_unresolvable(tmp_path):
    """and/or 守卫:不猜 → UnresolvableItem 保真,不出假关系。"""
    src = '''
import sqlite3
_DB = sqlite3.connect(":memory:")

def _load(oid):
    return _DB.execute("SELECT * FROM rows WHERE id=?", (oid,)).fetchone()

def handler(oid):
    row = _load(oid)
    if row is None:
        return "nf", 404
    if row["a"] == 1 and row["b"] == 2:
        return "early", 400
    _DB.execute("UPDATE rows SET done=1 WHERE id=?", (oid,))
    return "ok"
'''
    d = tmp_path / "boolop"
    d.mkdir()
    (d / "app.py").write_text(src, encoding="utf-8")
    ir = build_icfg(d)
    target = _find_block(ir, "handler", "UPDATE rows SET done=1")
    goal = derive_relational_goal(ir, target)
    assert any(u.cause == "guard-kind:boolop" for u in goal.unresolvable)
    assert not any("?" in u.what and ".a" in u.what for u in goal.unresolvable
                   if u.cause != "guard-kind:boolop")
    # 不产任何关于 a/b 的关系(不猜合取分支语义)
    assert all(".a" not in r.render() and ".b" not in r.render()
               for r in goal.relations)
