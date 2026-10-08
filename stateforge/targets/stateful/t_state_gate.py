"""t_state_gate：401 式守卫靶 —— 会话谓词不满足即被门拒，深块不可达。

P3-T2 C10 双 schema（与 t_state_chain 同约定）：
- {"steps": [...]}：按序重放；步1 login 置 role，步2 admin_panel 过门；
- legacy 单请求 dict：既有行为，先验状态默认空 → 恒被门拒（unauthorized）。

深块 gate_granted 只被"role=admin 的会话 + admin_panel"序列打开——这是
「等价单请求种子打不开、序列种子能打开」的旗舰验证靶。
"""

import json

_SESSION = {}      # 进程内会话态（跨 target() 调用持久）
HITS = []          # 分支命中留痕（函数名即标记；测试断言用）


def reset_state() -> None:
    _SESSION.clear()
    HITS.clear()


def login_ok():
    return "login"


def gate_granted():
    return "admin-panel"


def unauthorized():
    return 401


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
        role = body.get("role", "")
        if isinstance(role, str) and role:
            _SESSION["role"] = role
            HITS.append("login_ok")
            login_ok()
    elif op == "admin_panel":
        if _SESSION.get("role") == "admin":     # 401 式守卫：会话谓词判定
            HITS.append("gate_granted")
            gate_granted()
        else:
            HITS.append("unauthorized")
            unauthorized()
