"""t_state_chain：2 步状态链靶 —— 步1 置会话态，步2 深块仅在态已置时可达。

P3-T2 C10 双 schema：
- {"steps": [...]}：按序重放（驱动分派后每步一次 target 调用；靶本体收到
  整体 steps 时也可内部重放，语义等价）；
- legacy 单请求 dict：既有行为，先验状态 = 进程内当前态（默认空）。

会话连续性 = 模块级 _SESSION 进程内持久（同 adapter 持有 app/client 的
进程内语义）；reset_state() 供测试与多会话隔离。深块 deep_state_profile
只被"先 login 后 profile"的序列打开——单请求词法无论怎么变异都摸不到。
"""

import json

_SESSION = {}      # 进程内会话态（跨 target() 调用持久）
HITS = []          # 分支命中留痕（函数名即标记；测试断言用）


def reset_state() -> None:
    _SESSION.clear()
    HITS.clear()


def login_ok():
    return "login"


def deep_state_profile():
    return "stateful-profile"


def profile_anon():
    return "anon-profile"


def target(data: bytes) -> None:
    try:
        seed = json.loads(data) if data else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return                                  # 靶错误路径：非 JSON 输入
    if isinstance(seed, dict) and isinstance(seed.get("steps"), list):
        for step in seed["steps"]:              # 整体重放（等价于驱动逐步分派）
            _handle(step)
        return
    _handle(seed)


def _handle(seed) -> None:
    if not isinstance(seed, dict):
        return                                  # 靶错误路径：非 dict 请求
    body = seed.get("json", seed)
    if not isinstance(body, dict):
        return                                  # 靶错误路径：段坏
    op = str(body.get("op", ""))
    if op == "login":
        user = body.get("user", "")
        if isinstance(user, str) and user:
            _SESSION["uid"] = user
            HITS.append("login_ok")
            login_ok()
    elif op == "profile":
        if _SESSION.get("uid"):                 # 深块门：仅步1已置态时可达
            HITS.append("deep_state_profile")
            deep_state_profile()
        else:
            HITS.append("profile_anon")
            profile_anon()
