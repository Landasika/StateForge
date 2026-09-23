"""vulnshop —— 刻意脆弱的 Flask 商店靶场(SF0-T4,ADR-0005 单源码双变体)。

GUARDS=1(环境旗标)启用全部守卫(fixed 变体);GUARDS=0 暴露 5 类植入漏洞:

  (a) workflow_bypass  POST /orders/<id>/ship   无 status==PAID 前置
  (b) coupon_reuse     POST /orders             redeem 不查不置 coupon.used
  (c) idor             GET/POST /orders/<id>*   读单/支付/取消缺 owner 检查
  (d) payment_bypass   POST /orders/<id>/pay    amount<total 仍置 PAID
  (e) privilege        POST /admin/users/<id>/role  无 admin 角色检查

技术约束:文件 SQLite(stdlib sqlite3,非 WAL——L2 模板前提)+ stdlib hmac token;
守卫形态统一 `if guards: <check>`(SF1 patterns 的解析约定)。
"""

from __future__ import annotations

import hmac
import hashlib
import os
import sqlite3

from flask import Flask, jsonify, request

GUARDS = int(os.getenv("GUARDS", "0"))
DB_PATH = os.path.abspath(
    os.getenv("VULNSHOP_DB", os.path.join(os.path.dirname(__file__), "vulnshop.db"))
)
# 靶场常量 secret:token 跨进程重启可复现(确定性重放的前提)。
TOKEN_SECRET = b"vulnshop-dev-secret"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user'
);
CREATE TABLE IF NOT EXISTS orders(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    total INTEGER NOT NULL,
    paid_amount INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS coupons(
    code TEXT PRIMARY KEY,
    user_id INTEGER,
    used INTEGER NOT NULL DEFAULT 0,
    discount INTEGER NOT NULL
);
"""

SEED_COUPONS = [("SAVE10", 10), ("WELCOME5", 5)]


# ---------------------------------------------------------------- 底层设施


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    mode = conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
    assert mode.lower() != "wal", f"L2 前提违反: journal_mode={mode}"
    conn.row_factory = sqlite3.Row
    return conn


def init_database(db_path: str = None) -> None:
    conn = connect(db_path or DB_PATH)
    conn.executescript(SCHEMA)
    for code, discount in SEED_COUPONS:
        conn.execute(
            "INSERT OR IGNORE INTO coupons(code, user_id, used, discount) VALUES(?, NULL, 0, ?)",
            (code, discount),
        )
    conn.close()


def make_token(username: str) -> str:
    sig = hmac.new(TOKEN_SECRET, username.encode(), hashlib.sha256).hexdigest()
    return f"{username}:{sig}"


def current_db_path(app: Flask) -> str:
    return app.config["DB_PATH"]


def _bad_request(msg: str):
    return jsonify({"error": msg}), 400


# ---------------------------------------------------------------- 应用工厂


def create_app(db_path: str = None, guards: int = None, init_db: bool = True) -> Flask:
    guards = GUARDS if guards is None else guards
    app = Flask(__name__)
    app.config.update(GUARDS=guards, DB_PATH=db_path or DB_PATH)
    if init_db:
        init_database(app.config["DB_PATH"])

    def db():
        return connect(app.config["DB_PATH"])

    def current_user():
        """Bearer token(username:hmac_sig)→ users 行;无效返回 None。"""
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return None
        token = header[len("Bearer "):]
        username, _, sig = token.partition(":")
        if not username or not sig:
            return None
        if not hmac.compare_digest(make_token(username), token):
            return None
        return db().execute(
            "SELECT * FROM users WHERE username=?", (username,)
        ).fetchone()

    def require_user():
        user = current_user()
        if user is None:
            return None, (jsonify({"error": "unauthorized"}), 401)
        return user, None

    def get_order_or_none(oid: int):
        return db().execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()

    # ------------------------------------------------------------ 路由

    @app.get("/")
    def index():
        return jsonify(
            {"app": "vulnshop", "guards": guards, "coupons": [c for c, _ in SEED_COUPONS]}
        )

    @app.post("/auth/register")
    def register():
        body = request.get_json(silent=True) or {}
        username, password = body.get("username"), body.get("password")
        if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
            return _bad_request("username/password required")
        conn = db()
        try:
            cur = conn.execute(
                "INSERT INTO users(username, password, role) VALUES(?, ?, 'user')",
                (username, password),
            )
        except sqlite3.IntegrityError:
            return jsonify({"error": "username exists"}), 409
        return jsonify({"id": cur.lastrowid, "username": username, "role": "user"}), 201

    @app.post("/auth/login")
    def login():
        body = request.get_json(silent=True) or {}
        row = db().execute(
            "SELECT * FROM users WHERE username=? AND password=?",
            (body.get("username"), body.get("password")),
        ).fetchone()
        if row is None:
            return jsonify({"error": "bad credentials"}), 401
        return jsonify({"token": make_token(row["username"])})

    @app.get("/auth/me")
    def me():
        user, err = require_user()
        if err:
            return err
        return jsonify({"id": user["id"], "username": user["username"], "role": user["role"]})

    @app.post("/orders")
    def create_order():
        user, err = require_user()
        if err:
            return err
        body = request.get_json(silent=True) or {}
        items = body.get("items")
        if not isinstance(items, list) or not items:
            return _bad_request("items required")
        total = 0
        for item in items:
            if not isinstance(item, dict):
                return _bad_request("bad item")
            price, qty = item.get("price"), item.get("qty")
            if not isinstance(price, int) or price <= 0 or not isinstance(qty, int) or qty <= 0:
                return _bad_request("bad price/qty")
            total += price * qty

        coupon_code = body.get("coupon_code")
        if coupon_code is not None:
            coupon = db().execute(
                "SELECT * FROM coupons WHERE code=?", (coupon_code,)
            ).fetchone()
            if coupon is None:
                return jsonify({"error": "coupon not found"}), 404
            if guards:  # 守卫 (b):单次性检查
                if coupon["used"]:
                    return jsonify({"error": "coupon already used"}), 409
            total -= coupon["discount"]
            # 置位恒发生(非守卫):used 必须翻转一次,漏洞才可被 oracle 观测——
            # vuln = "不查"(used==1 时仍应用折扣),不是"不置"(状态永不变化则
            # InvariantOracle/注解/SF3 goal_coupon_reuse 全部失效)
            db().execute(
                "UPDATE coupons SET used=1, user_id=? WHERE code=?",
                (user["id"], coupon_code),
            )

        conn = db()
        cur = conn.execute(
            "INSERT INTO orders(user_id, status, total, paid_amount) VALUES(?, 'CREATED', ?, 0)",
            (user["id"], total),
        )
        return (
            jsonify({"id": cur.lastrowid, "status": "CREATED", "total": total, "paid_amount": 0}),
            201,
        )

    @app.get("/orders/<int:oid>")
    def read_order(oid: int):
        user, err = require_user()
        if err:
            return err
        order = get_order_or_none(oid)
        if order is None:
            return jsonify({"error": "order not found"}), 404
        if guards:  # 守卫 (c):owner 检查
            if order["user_id"] != user["id"]:
                return jsonify({"error": "not your order"}), 403
        return jsonify(
            {
                "id": order["id"],
                "user_id": order["user_id"],
                "status": order["status"],
                "total": order["total"],
                "paid_amount": order["paid_amount"],
            }
        )

    @app.post("/orders/<int:oid>/pay")
    def pay_order(oid: int):
        user, err = require_user()
        if err:
            return err
        order = get_order_or_none(oid)
        if order is None:
            return jsonify({"error": "order not found"}), 404
        if guards:  # 守卫 (c):owner 检查
            if order["user_id"] != user["id"]:
                return jsonify({"error": "not your order"}), 403
        amount = (request.get_json(silent=True) or {}).get("amount")
        if not isinstance(amount, int) or amount <= 0:
            return _bad_request("amount must be positive int")
        paid = order["paid_amount"] + amount
        if guards:  # 守卫 (d):付满才 PAID
            status = "PAID" if paid >= order["total"] else order["status"]
        else:
            status = "PAID"
        db().execute(
            "UPDATE orders SET paid_amount=?, status=? WHERE id=?", (paid, status, oid)
        )
        return jsonify({"id": oid, "status": status, "paid_amount": paid})

    @app.post("/orders/<int:oid>/ship")
    def ship_order(oid: int):
        user, err = require_user()
        if err:
            return err
        order = get_order_or_none(oid)
        if order is None:
            return jsonify({"error": "order not found"}), 404
        if guards:  # 守卫 (a)+(c):owner + PAID 前置
            if order["user_id"] != user["id"]:
                return jsonify({"error": "not your order"}), 403
            if order["status"] != "PAID":
                return jsonify({"error": "order not paid"}), 409
        db().execute("UPDATE orders SET status='SHIPPED' WHERE id=?", (oid,))
        return jsonify({"id": oid, "status": "SHIPPED"})

    @app.post("/orders/<int:oid>/cancel")
    def cancel_order(oid: int):
        user, err = require_user()
        if err:
            return err
        order = get_order_or_none(oid)
        if order is None:
            return jsonify({"error": "order not found"}), 404
        if guards:  # 守卫 (c):owner 检查
            if order["user_id"] != user["id"]:
                return jsonify({"error": "not your order"}), 403
        db().execute("UPDATE orders SET status='CANCELLED' WHERE id=?", (oid,))
        return jsonify({"id": oid, "status": "CANCELLED"})

    @app.get("/coupons/<code>")
    def read_coupon(code: str):
        row = db().execute("SELECT * FROM coupons WHERE code=?", (code,)).fetchone()
        if row is None:
            return jsonify({"error": "coupon not found"}), 404
        return jsonify(
            {"code": row["code"], "used": row["used"], "discount": row["discount"]}
        )

    @app.post("/admin/users/<int:uid>/role")
    def set_role(uid: int):
        user, err = require_user()
        if err:
            return err
        if guards:  # 守卫 (e):admin 检查
            if user["role"] != "admin":
                return jsonify({"error": "admin required"}), 403
        target = db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if target is None:
            return jsonify({"error": "user not found"}), 404
        role = (request.get_json(silent=True) or {}).get("role")
        if not isinstance(role, str) or not role:
            return _bad_request("role required")
        db().execute("UPDATE users SET role=? WHERE id=?", (role, uid))
        return jsonify({"id": uid, "role": role})

    return app


app = create_app()


def main() -> None:  # 直接 `python app.py` 起服(同步单线程,fork-per-leaf 前提)
    from werkzeug.serving import make_server

    port = int(os.getenv("VULNSHOP_PORT", "5000"))
    host = os.getenv("VULNSHOP_HOST", "0.0.0.0")
    server = make_server(host, port, app, threaded=False)
    print(f"[vulnshop] GUARDS={GUARDS} db={DB_PATH} listening on {host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
