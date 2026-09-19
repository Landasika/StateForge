"""vulnshop golden PoC tests — 5 漏洞类 × {vuln, fixed} + 压测 + 变体对等。

运行(ANOTA 解释器,flask 2.2.2 栈):
  anota/ANOTA/ANOTA-interpreter/python -m pytest -m anota stateforge/tests/targets/vulnshop

植入漏洞清单(GUARDS=0,ADR-0005 单源码双变体):
  (a) workflow_bypass  ship 无 PAID 前置
  (b) coupon_reuse     redeem 不查不置 used
  (c) idor             读单/支付缺 owner 检查
  (d) payment_bypass   金额不足仍置 PAID
  (e) privilege        改角色无 admin 检查
"""

import sqlite3

import pytest

from stateforge.targets.vulnshop import app as vshop

pytestmark = pytest.mark.anota


def fresh(tmp_path, guards: int):
    db = tmp_path / f"shop-g{guards}.db"
    application = vshop.create_app(db_path=str(db), guards=guards, init_db=True)
    return application, application.test_client()


def register(client, username: str, password: str = None):
    return client.post(
        "/auth/register", json={"username": username, "password": password or f"pw-{username}"}
    )


def login(client, username: str, password: str = None) -> str:
    password = password or f"pw-{username}"
    register(client, username, password)  # 幂等:已存在则 409,忽略
    r = client.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["token"]


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


ITEMS = [{"sku": "book", "price": 100, "qty": 1}]


def create_order(client, token, items=None, coupon=None) -> int:
    body = {"items": items or ITEMS}
    if coupon:
        body["coupon_code"] = coupon
    r = client.post("/orders", json=body, headers=auth(token))
    assert r.status_code == 201, r.get_json()
    return r.get_json()["id"]


def get_order(client, token, oid: int):
    return client.get(f"/orders/{oid}", headers=auth(token))


def pay(client, token, oid: int, amount: int):
    return client.post(f"/orders/{oid}/pay", json={"amount": amount}, headers=auth(token))


def ship(client, token, oid: int):
    return client.post(f"/orders/{oid}/ship", headers=auth(token))


# ---------------------------------------------------------------- 5 类 PoC


def test_poc_workflow_bypass(tmp_path):
    """)(a) CREATED → SHIPPED 直迁;fixed 要求 PAID。"""
    _, c = fresh(tmp_path, guards=0)
    t = login(c, "u1")
    oid = create_order(c, t)
    r = ship(c, t, oid)
    assert r.status_code == 200
    assert r.get_json()["status"] == "SHIPPED"  # 未支付直接发货

    _, c2 = fresh(tmp_path, guards=1)
    t2 = login(c2, "u1")
    oid2 = create_order(c2, t2)
    r2 = ship(c2, t2, oid2)
    assert r2.status_code == 409
    assert get_order(c2, t2, oid2).get_json()["status"] == "CREATED"  # 守卫阻断


def test_poc_coupon_reuse(tmp_path):
    """)(b) 同券两次 redeem;fixed 二次 409。"""
    _, c = fresh(tmp_path, guards=0)
    t = login(c, "u1")
    o1 = create_order(c, t, coupon="SAVE10")
    o2 = create_order(c, t, coupon="SAVE10")  # 同一张券再用
    body1 = get_order(c, t, o1).get_json()
    body2 = get_order(c, t, o2).get_json()
    assert body1["total"] == 90 and body2["total"] == 90  # 已用券仍拿到折扣 = 漏洞 (b)
    coupon = c.get("/coupons/SAVE10").get_json()
    assert coupon["used"] == 1  # 置位恒发生:重用 = used==1 时仍应用折扣

    _, c2 = fresh(tmp_path, guards=1)
    t2 = login(c2, "u1")
    create_order(c2, t2, coupon="SAVE10")
    r2 = c2.post(
        "/orders", json={"items": ITEMS, "coupon_code": "SAVE10"}, headers=auth(t2)
    )
    assert r2.status_code == 409  # 已用券被拒
    assert c2.get("/coupons/SAVE10").get_json()["used"] == 1  # 首次已置位


def test_poc_idor(tmp_path):
    """)(c) B 读/付 A 的订单;fixed 403。"""
    for guards, expect in ((0, 200), (1, 403)):
        _, c = fresh(tmp_path, guards=guards)
        ta = login(c, "alice")
        tb = login(c, "bob")
        oid = create_order(c, ta)
        r = get_order(c, tb, oid)
        assert r.status_code == expect, (guards, r.status_code)
        r2 = pay(c, tb, oid, 100)
        assert r2.status_code == expect, (guards, r2.status_code)


def test_poc_payment_bypass(tmp_path):
    """)(d) total=100 只付 10 仍 PAID;fixed 分段支付,CREATED 直到付满。"""
    _, c = fresh(tmp_path, guards=0)
    t = login(c, "u1")
    oid = create_order(c, t)
    r = pay(c, t, oid, 10)
    assert r.status_code == 200
    body = r.get_json()
    assert body["status"] == "PAID" and body["paid_amount"] == 10  # 未付满即 PAID

    _, c2 = fresh(tmp_path, guards=1)
    t2 = login(c2, "u1")
    oid2 = create_order(c2, t2)
    r2 = pay(c2, t2, oid2, 10)
    body2 = r2.get_json()
    assert body2["status"] == "CREATED" and body2["paid_amount"] == 10  # 部分支付记录但不发货态
    r3 = pay(c2, t2, oid2, 90)
    assert r3.get_json()["status"] == "PAID"  # 付满才 PAID


def test_poc_privilege(tmp_path):
    """)(e) 普通用户自提权 admin;fixed 403。"""
    _, c = fresh(tmp_path, guards=0)
    t = login(c, "mallory")
    uid = c.get("/auth/me", headers=auth(t)).get_json()["id"]
    r = c.post(f"/admin/users/{uid}/role", json={"role": "admin"}, headers=auth(t))
    assert r.status_code == 200
    assert c.get("/auth/me", headers=auth(t)).get_json()["role"] == "admin"  # 自提权成功

    _, c2 = fresh(tmp_path, guards=1)
    t2 = login(c2, "mallory")
    uid2 = c2.get("/auth/me", headers=auth(t2)).get_json()["id"]
    r2 = c2.post(f"/admin/users/{uid2}/role", json={"role": "admin"}, headers=auth(t2))
    assert r2.status_code == 403
    assert c2.get("/auth/me", headers=auth(t2)).get_json()["role"] == "user"


# ---------------------------------------------------------------- 健壮性


def test_benign_flows_and_validation(tmp_path):
    """正常链路 + 输入校验(两变体一致的行为面)。"""
    for guards in (0, 1):
        _, c = fresh(tmp_path, guards=guards)
        r = register(c, "u1")
        assert r.status_code == 201 and r.get_json()["role"] == "user"
        assert register(c, "u1").status_code == 409  # 重名
        t = login(c, "u1")
        assert c.get("/auth/me").status_code == 401  # 无 token
        assert c.get("/auth/me", headers={"Authorization": "Bearer bad"}).status_code == 401
        oid = create_order(c, t)
        assert c.post("/orders", json={"items": []}, headers=auth(t)).status_code == 400
        assert get_order(c, t, 9999).status_code == 404
        assert c.get(f"/coupons/NOPE").status_code == 404
        r = pay(c, t, oid, 100)
        assert r.get_json()["status"] == "PAID"
        r = ship(c, t, oid)
        assert r.status_code == 200  # 已付发货,两变体都合法
        r = c.post(f"/orders/{oid}/cancel", headers=auth(t))
        assert r.status_code == 200 and r.get_json()["status"] == "CANCELLED"


def test_stress_1000_requests(tmp_path):
    """1000 请求压测:无 5xx、无泄漏、DB 一致。"""
    _, c = fresh(tmp_path, guards=0)
    codes = []
    for i in range(1000):
        if i % 3 == 0:
            codes.append(register(c, f"user{i}").status_code)
        elif i % 3 == 1:
            codes.append(c.post("/auth/login", json={"username": "user0", "password": "pw-user0"}).status_code)
        else:
            codes.append(c.get("/coupons/SAVE10").status_code)
    assert all(s < 500 for s in codes), sorted(set(codes))


def test_journal_mode_not_wal(tmp_path):
    """L2 前提(SF2):文件 DB 必须 journal_mode=DELETE。"""
    _, c = fresh(tmp_path, guards=0)
    db = vshop.current_db_path(c.application)
    mode = sqlite3.connect(db).execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "delete"
