"""探针公共库 —— stdlib urllib 直连已服务的 VAmPI。"""

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.getenv("VAMPI_BASE", "http://127.0.0.1:5000")
VULN = int(os.getenv("VAMPI_VULN", "1"))


def call(method: str, path: str, body: dict = None, token: str = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def jbody(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text}


def uniq(prefix: str) -> str:
    import time

    return f"{prefix}{int(time.time() * 1000) % 100000000}"


def register_and_login(username: str):
    """注册 + 登录,返回 (username, token);失败则抛 SystemExit。"""
    status, body = call(
        "POST", "/users/v1/register",
        {"username": username, "password": f"pw-{username}", "email": f"{username}@x.io"},
    )
    if status not in (200, 201):
        raise SystemExit(f"register failed: {status} {body}")
    status, body = call(
        "POST", "/users/v1/login", {"username": username, "password": f"pw-{username}"}
    )
    token = jbody(body).get("auth_token")
    if not token:
        raise SystemExit(f"login failed: {status} {body}")
    return username, token


def verdict(name: str, vulnerable_behavior: bool):
    """行为与运行态一致性判定:vuln=1 期望漏洞行为,vuln=0 期望阻断。"""
    ok = vulnerable_behavior == bool(VULN)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: "
          f"observed={'vulnerable' if vulnerable_behavior else 'guarded'} expected_vuln={VULN}")
    return 0 if ok else 1


def run(main):
    sys.exit(main())
