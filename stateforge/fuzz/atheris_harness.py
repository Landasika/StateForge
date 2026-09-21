"""atheris_harness.py —— Atheris fuzz harness(进程内模式)。

Atheris(libFuzzer)自动变异字节 → 我们解码成 HTTP 动作 → 进程内调 WSGI。
覆盖反馈: libFuzzer 8-bit 计数器(每基本块一个字节)。

运行(必须用 ANOTA 解释器,已装 atheris 2.0.12):
  ANOTA_PY=~/icassp/anota/ANOTA/ANOTA-interpreter/python
  $ANOTA_PY -m stateforge.fuzz.atheris_harness stateforge/targets/vulnshop

产物: crash-* 文件(触发 ANOTA_EXECUTION.BLOCK 的输入) + 语料目录 corpus/
"""

from __future__ import annotations

import os
import re
import struct
import sys
from pathlib import Path

# ---------------------------------------------------------------- 加载目标(必须在 instrument_all 之前)

TARGET_DIR = sys.argv[1] if len(sys.argv) > 1 else "stateforge/targets/vulnshop"
FRAMEWORK = "flask"

sys.path.insert(0, str(Path(TARGET_DIR).parent))
_mod = Path(TARGET_DIR).name

if FRAMEWORK == "flask":
    from importlib import import_module
    _app_mod = import_module(f"{_mod}.app")
    APP = _app_mod.create_app(db_path="/tmp/atheris_fuzz.db", guards=0, init_db=True)
    CLIENT = APP.test_client()
else:
    raise RuntimeError(f"framework {FRAMEWORK} not supported")

# ---------------------------------------------------------------- Atheris(必须在 app 加载后)

import atheris
atheris.instrument_all()  # 此时 app 模块已加载,会被正确插桩

CORPUS_DIR = str((Path(__file__).parent.parent.parent / "corpus").resolve())
Path(CORPUS_DIR).mkdir(exist_ok=True)

# ---------------------------------------------------------------- 动作词汇表

ACTIONS = []
for rule in APP.url_map.iter_rules():
    if rule.endpoint == "static":
        continue
    methods = [m for m in rule.methods if m not in ("HEAD", "OPTIONS")]
    route = rule.rule
    clean = re.sub(r"<(?:int:|string:|path:)?(\w+)>", r"{\1}", route)
    for m in methods:
        ACTIONS.append({"method": m, "route": clean})

# 常量参数池(fuzz 会从中选值)
PARAM_POOL = {
    "username": ["admin", "user1", "test_user", "sf_fuzz"],
    "password": ["admin", "pass123", "password", ""],
    "items": [[{"sku": "book", "price": 100, "qty": 1}], []],
    "amount": [100, 50, -1, 0, 999999],
    "coupon_code": ["SAVE10", "WELCOME5", "INVALID", ""],
    "role": ["admin", "user", "moderator", ""],
    # 路径参数
    "oid": [1, 2, 999, 0],
    "uid": [1, 2, 999],
    "code": ["SAVE10", "WELCOME5", "X"],
}

# ---------------------------------------------------------------- 编码/解码


def decode_action(data: bytes) -> dict | None:
    """fuzz 字节 → HTTP 动作。

    编码: [action_idx:u16] [param_region]
    param_region: 剩余字节用于从参数池选择值
    """
    if len(data) < 2:
        return None
    action_idx = struct.unpack("<H", data[:2])[0]
    if action_idx >= len(ACTIONS):
        action_idx = action_idx % len(ACTIONS)
    action = ACTIONS[action_idx].copy()
    param_bytes = data[2:]

    # 路径参数填充
    import re
    route = action["route"]
    path_params = re.findall(r"{(\w+)}", route)
    for i, pp in enumerate(path_params):
        if i * 2 + 1 < len(param_bytes):
            val_idx = param_bytes[i * 2] % len(PARAM_POOL.get(pp, [1]))
            route = route.replace(f"{{{pp}}}", str(PARAM_POOL[pp][val_idx]))
        else:
            route = route.replace(f"{{{pp}}}", "1")
    action["route"] = route

    # JSON 参数(POST/PUT)
    if action["method"] in ("POST", "PUT", "PATCH"):
        params = {}
        # 从剩余字节推导参数
        offset = len(path_params) * 2
        remaining = param_bytes[offset:]
        for pname in ("username", "password", "amount", "coupon_code", "role", "items"):
            pool = PARAM_POOL.get(pname)
            if not pool:
                continue
            # 用一个字节选值
            if offset < len(param_bytes):
                idx = param_bytes[offset] % len(pool)
                params[pname] = pool[idx]
                offset += 1
        action["json"] = params

    return action


# ---------------------------------------------------------------- 认证 token 缓存

_token_cache = {"token": None}


def get_token():
    """获取或缓存认证 token。"""
    if _token_cache["token"]:
        return _token_cache["token"]
    resp = CLIENT.post("/auth/register", json={
        "username": f"fuzz_{os.getpid()}",
        "password": "fuzz_pass"})
    resp = CLIENT.post("/auth/login", json={
        "username": f"fuzz_{os.getpid()}",
        "password": "fuzz_pass"})
    if resp.status_code == 200:
        _token_cache["token"] = resp.get_json().get("token")
    return _token_cache["token"]


# ---------------------------------------------------------------- TestOneInput


def TestOneInput(data: bytes):
    """Atheris 每次调用此函数,给一段随机字节。"""
    action = decode_action(data)
    if action is None:
        return

    headers = {}
    # 有概率带 token(增加到达深层代码的机会)
    if data and data[0] % 3 != 0:  # 2/3 的请求带 auth
        token = get_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"

    try:
        method = action["method"]
        route = action["route"]
        if method in ("POST", "PUT", "PATCH") and action.get("json"):
            CLIENT.open(route, method=method, json=action["json"], headers=headers)
        else:
            CLIENT.open(route, method=method, headers=headers)
    except Exception:
        # 异常由 Atheris 记录为 crash(如果开了异常检测)
        pass


# ---------------------------------------------------------------- 启动

if __name__ == "__main__":
    print(f"[atheris] target={TARGET_DIR} actions={len(ACTIONS)}")
    print(f"[atheris] corpus dir: {CORPUS_DIR}")
    sys.argv = [sys.argv[0], CORPUS_DIR, "-max_total_time=30"]
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()
