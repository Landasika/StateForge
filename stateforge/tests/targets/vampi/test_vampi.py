"""VAmPI 冒烟:ANOTA 下双态可服务 + 建库 + 基本端点 + 登录链。

真实端口(127.0.0.1:5001,werkzeug 同步单线程)+ stdlib urllib(免 httpx 依赖)。

实测靶场怪癖(2026-09-18):
- 路由挂根(servers 是绝对 URL,无 /v1 前缀);/createdb 是 GET
- users/books 响应是带键字典({"users": [...]} / {"Books": [...]})
- populate_db 非幂等(第 3 次起 UNIQUE 约束炸)——夹具先删 database.db 保确定性
- admin 种子口令 pass1;login 失败也回 200 + {"status":"fail"},须查 status 字段

双态:vulnerable 旗标 import 时读取 → 环境变量 VAMPI_VULN 控制,跑两遍:
  VAMPI_VULN=1 <anota-py> -m pytest -m anota .../test_vampi.py
  VAMPI_VULN=0 同上
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request

import pytest

from stateforge.targets.vampi import adapter

pytestmark = pytest.mark.anota

VULN = int(os.getenv("VAMPI_VULN", "1"))
PORT = 5001
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture(scope="module")
def served():
    db_file = adapter.VAMPI_DIR / "database" / "database.db"
    db_file.unlink(missing_ok=True)  # populate_db 非幂等 → 从零开始
    flask_app = adapter.make_app(vulnerable=VULN, token_ttl=600)
    server = adapter._make_server(flask_app, "127.0.0.1", PORT)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)
    yield BASE
    server.shutdown()


def get(path: str, token: str = None):
    req = urllib.request.Request(BASE + path)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def post(path: str, body: dict, token: str = None):
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_healthy_root_reports_vuln_flag(served):
    status, body = get("/")
    assert status == 200
    payload = json.loads(body)
    assert payload["vulnerable"] == VULN  # 旗标与运行态一致


def test_createdb_and_user_flow(served):
    status, _ = get("/createdb")  # spec: /createdb 是 GET
    assert status == 200
    status, body = get("/users/v1")
    assert status == 200
    payload = json.loads(body)
    assert any(u.get("username") == "admin" for u in payload["users"])
    # 登录拿 token → /me(admin 种子口令 pass1)
    status, body = post("/users/v1/login", {"username": "admin", "password": "pass1"})
    assert status == 200 and json.loads(body).get("status") != "fail", body
    token = json.loads(body)["auth_token"]
    status, body = get("/me", token=token)
    assert status == 200 and json.loads(body)["data"]["username"] == "admin"


def test_books_endpoint(served):
    status, body = get("/books/v1")
    assert status == 200
    payload = json.loads(body)
    assert isinstance(payload["Books"], list) and payload["Books"]
