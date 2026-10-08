"""t_state_shop：3 步状态链靶 —— register → create_order → ship_order。

P3-T2 C10 双 schema（与 t_state_chain 同约定）：
- {"steps": [...]}：按序重放；注册建号建会话、下单依赖会话身份、发货
  依赖订单归属——三步全通才到特权块 ship_privileged；
- legacy 单请求 dict：既有行为，先验状态默认空 → 单步 create/ship 走
  靶错误路径（ship_denied）。

会话连续性 = 模块级 _SESSION/_DB 进程内持久；reset_state() 供测试隔离。

P3-T3：会话态以 M1 状态分析词汇书写（持有者名 `session`，`_SESSION` 为
同一 dict 的兼容别名）。诚实边界：本靶的深块守卫在 DB 域（订单归属），
M1 会话词汇看不到守卫谓词 → state 注入器对本靶零 goal（零注入），
这不是缺陷而是静态词汇边界的如实反映。
"""

import json

_SESSION = {}      # 进程内会话态（跨 target() 调用持久）
session = _SESSION  # M1 分析词汇：session 名下的同一 dict（别名不改行为）
_DB = {"users": {}, "orders": {}}   # 进程内"库"（订单归属判定的状态载体）
HITS = []          # 分支命中留痕（函数名即标记；测试断言用）


def reset_state() -> None:
    _SESSION.clear()
    _DB["users"].clear()
    _DB["orders"].clear()
    HITS.clear()


def register_ok():
    return "registered"


def order_created():
    return "order"


def ship_privileged():
    return "shipped"


def ship_denied():
    return 403


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
    if op == "register":
        user = body.get("user", "")
        password = body.get("password", "")
        if isinstance(user, str) and user and isinstance(password, str) and password:
            _DB["users"][user] = {"password": password}
            session["uid"] = user               # M1 词汇：session[...] 写
            HITS.append("register_ok")
            register_ok()
    elif op == "create_order":
        uid = session.get("uid")
        if uid in _DB["users"]:                 # 下单门：必须已注册会话
            oid = str(len(_DB["orders"]) + 1)
            _DB["orders"][oid] = {"owner": uid,
                                  "item": str(body.get("item", ""))}
            session["order_id"] = oid
            HITS.append("order_created")
            order_created()
    elif op == "ship_order":
        oid = body.get("order") or session.get("order_id")
        order = _DB["orders"].get(oid) if isinstance(oid, str) else None
        if order is None:
            HITS.append("ship_denied")          # 无单可发
            ship_denied()
        elif order["owner"] == session.get("uid"):
            HITS.append("ship_privileged")      # 特权块：归属+会话双判定
            ship_privileged()
        else:
            HITS.append("ship_denied")          # 越权：非本人订单
            ship_denied()
