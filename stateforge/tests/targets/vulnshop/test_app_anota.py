"""vulnshop 注解信道测试 —— ANOTA_EXECUTION.BLOCK 策略在 5 类攻击上触发并可回收。

只在 ANOTA 解释器下可跑(ANOTA_EXECUTION 在 builtins);其余环境整文件跳过。
回收方式与母本 exp/harness_dsvw.py:policies_in() 相同:正则扫响应体。
"""

import builtins
import re

import pytest

if "ANOTA_EXECUTION" not in dir(builtins):
    pytest.skip("ANOTA_EXECUTION 不在 builtins(非 ANOTA 解释器)", allow_module_level=True)

from stateforge.targets.vulnshop import app as vshop
from stateforge.targets.vulnshop import app_anota

pytestmark = pytest.mark.anota

POLICY_RE = re.compile(r"ANOTA_POLICY:(\w+)")


def policies_in(body: str):
    return POLICY_RE.findall(body)


def fresh(tmp_path):
    application = app_anota.create_anota_app(
        db_path=str(tmp_path / "shop-anota.db"), init_db=True
    )
    return application, application.test_client()


def register(client, username):
    return client.post("/auth/register", json={"username": username, "password": f"pw-{username}"})


def login(client, username):
    register(client, username)
    r = client.post("/auth/login", json={"username": username, "password": f"pw-{username}"})
    return r.get_json()["token"]


def auth(t):
    return {"Authorization": f"Bearer {t}"}


ITEMS = [{"sku": "book", "price": 100, "qty": 1}]


def test_benign_flow_fires_no_policy(tmp_path):
    """合法流量:注册→登录→建单→付满→发货,全程无策略触发。"""
    _, c = fresh(tmp_path)
    t = login(c, "nice")
    r = c.post("/orders", json={"items": ITEMS}, headers=auth(t))
    assert r.status_code == 201
    oid = r.get_json()["id"]
    r = c.post(f"/orders/{oid}/pay", json={"amount": 100}, headers=auth(t))
    assert r.status_code == 200 and r.get_json()["status"] == "PAID"
    r = c.post(f"/orders/{oid}/ship", headers=auth(t))
    assert r.status_code == 200
    assert policies_in(str(r.get_data())) == []


def test_policy_ship_requires_paid(tmp_path):
    _, c = fresh(tmp_path)
    t = login(c, "u1")
    oid = c.post("/orders", json={"items": ITEMS}, headers=auth(t)).get_json()["id"]
    r = c.post(f"/orders/{oid}/ship", headers=auth(t))
    assert r.status_code == 500
    assert "ship_requires_paid" in policies_in(str(r.get_data()))


def test_policy_coupon_single_use(tmp_path):
    _, c = fresh(tmp_path)
    t = login(c, "u1")
    r1 = c.post("/orders", json={"items": ITEMS, "coupon_code": "SAVE10"}, headers=auth(t))
    assert r1.status_code == 201  # 首次合法
    r2 = c.post("/orders", json={"items": ITEMS, "coupon_code": "SAVE10"}, headers=auth(t))
    assert r2.status_code == 500
    assert "coupon_single_use" in policies_in(str(r2.get_data()))


def test_policy_order_owner_read_pay(tmp_path):
    _, c = fresh(tmp_path)
    ta = login(c, "alice")
    tb = login(c, "bob")
    oid = c.post("/orders", json={"items": ITEMS}, headers=auth(ta)).get_json()["id"]
    r_read = c.get(f"/orders/{oid}", headers=auth(tb))
    assert r_read.status_code == 500
    assert "order_owner_read" in policies_in(str(r_read.get_data()))
    r_pay = c.post(f"/orders/{oid}/pay", json={"amount": 100}, headers=auth(tb))
    assert r_pay.status_code == 500
    assert "order_owner_pay" in policies_in(str(r_pay.get_data()))


def test_policy_pay_full_amount(tmp_path):
    _, c = fresh(tmp_path)
    t = login(c, "u1")
    oid = c.post("/orders", json={"items": ITEMS}, headers=auth(t)).get_json()["id"]
    r = c.post(f"/orders/{oid}/pay", json={"amount": 10}, headers=auth(t))
    assert r.status_code == 500
    assert "pay_full_amount" in policies_in(str(r.get_data()))


def test_policy_admin_required(tmp_path):
    _, c = fresh(tmp_path)
    t = login(c, "mallory")
    uid = c.get("/auth/me", headers=auth(t)).get_json()["id"]
    r = c.post(f"/admin/users/{uid}/role", json={"role": "admin"}, headers=auth(t))
    assert r.status_code == 500
    assert "admin_required" in policies_in(str(r.get_data()))


def test_route_parity_with_plain_app(tmp_path):
    """注解变体与普通变体路由集合一致(漂移防线)。"""
    plain = vshop.create_app(db_path=str(tmp_path / "p.db"), guards=0)
    anota = app_anota.create_anota_app(db_path=str(tmp_path / "a.db"))
    rules = lambda app: sorted(
        r.rule for r in app.url_map.iter_rules() if r.endpoint != "static"
    )
    assert rules(plain) == rules(anota)
