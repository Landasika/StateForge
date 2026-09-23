"""Phase-1 acceptance fixture(spec §4 P1-8)。

形态刻意覆盖:同块双调用(get_user+log_access)、尾部调用(return checkout(user))、
分支内调用(checkout 的 return charge(user))、外部属性调用(request.headers.get,
应落 unresolved 而非假边)。
"""
from flask import Flask, request

app = Flask(__name__)

USERS: dict = {}          # 测试注入 {name: SimpleNamespace(balance=…)}


@app.post("/buy")
def buy():
    user = get_user()
    log_access()
    if not user:
        return "unauthorized", 401
    return checkout(user)


def get_user():
    return USERS.get(request.headers.get("X-User"))


def log_access():
    return None


def checkout(user):
    if user.balance > 100:
        return charge(user)
    return "insufficient"


def charge(user):
    return "charged"
