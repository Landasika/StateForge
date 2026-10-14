"""诊断 2 回归:ne→eq 反转仅限 guard-return 语境。

非 guard-return 的 `if order["status"] != V: <正常分支体>` 分支体语义是
"≠ V",状态原子词汇(eq 形字符串)不可表达——R3 只记 candidate、不产
precondition;guard-return 的 `if order["status"] != V: return` 持续执行
条件才是 == V(既有惯用法,builder._pre_atom 反转,vulnshop ship=PAID
机制依赖此路径);abort() 早退同属 guard-return 族。
"""

from pathlib import Path

import networkx as nx

from stateforge.analysis.analyzer import analyze
from stateforge.analysis.builder import build_sstg

_APP = '''
from flask import Flask, jsonify, abort

app = Flask(__name__)


def db():
    raise NotImplementedError


@app.get("/branch-entry")
def branch_entry():
    order = db().execute("SELECT * FROM orders").fetchone()
    total = 0
    if order["status"] != "CANCELLED":
        total = total + 1
    return jsonify({"ok": order["status"], "total": total})


@app.post("/ship")
def ship():
    order = db().execute("SELECT * FROM orders").fetchone()
    if order["status"] != "PAID":
        return jsonify({"error": "order not paid"}), 409
    db().execute("UPDATE orders SET status='SHIPPED'")
    return jsonify({"status": "SHIPPED"})


@app.get("/admin-only")
def admin_only():
    user = db().execute("SELECT * FROM users").fetchone()
    if user["role"] != "admin":
        abort(403)
    return jsonify({"secret": "yes"})
'''


def _model(tmp_path: Path):
    (tmp_path / "app.py").write_text(_APP, encoding="utf-8")
    return analyze(tmp_path, "flask")


def test_non_guard_ne_produces_no_precondition(tmp_path):
    model = _model(tmp_path)
    ep = next(e for e in model.endpoints if e.route == "/branch-entry")
    # 分支体非早退:ne 不得产 precondition(不可表达,而非错误反转成 ==)
    assert not any(p.op == "ne" for p in ep.preconditions)
    # candidate 仍留给标注层
    assert any(c.var == "Order.status" for c in model.candidates)


def test_guard_return_ne_keeps_precondition_and_atom(tmp_path):
    model = _model(tmp_path)
    ep = next(e for e in model.endpoints if e.route == "/ship")
    assert any(p.op == "ne" and p.value == "PAID" for p in ep.preconditions)
    sstg = build_sstg(model)
    ship = next(n for n, d in sstg.nodes(data=True)
                if d.get("kind") == "action" and "ship" in n)
    requires = {v for _, v, d in sstg.edges(ship, data=True)
                if d["kind"] == "requires"}
    assert "Order.status=PAID" in requires  # guard-return 归一(既有机制)


def test_guard_return_abort_counts(tmp_path):
    """Flask abort() 早退同属 guard-return(raise 族)。"""
    model = _model(tmp_path)
    ep = next(e for e in model.endpoints if e.route == "/admin-only")
    assert any(p.op == "ne" and p.value == "admin" for p in ep.preconditions)


def test_sstg_has_no_wrong_inversion_atom(tmp_path):
    """branch-entry 的 ne 不得变成 requires Order.status=CANCELLED。"""
    sstg: nx.MultiDiGraph = build_sstg(_model(tmp_path))
    branch = next(n for n, d in sstg.nodes(data=True)
                  if d.get("kind") == "action" and "branch-entry" in n)
    requires = {v for _, v, d in sstg.edges(branch, data=True)
                if d["kind"] == "requires"}
    assert "Order.status=CANCELLED" not in requires
