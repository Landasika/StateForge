"""microblog 靶 harness(Miguel Grinberg Mega-Tutorial 完整应用,批 1)。

形态:external/microblog 源码(app 包工厂 create_app())+ external/microblog/
.venv → sqlite 每进程路径(SF_DB env 或默认含 pid)→ ORM db.create_all()
+ 种子 user → test_client POST /auth/login 引导登录会话。

配置约定(config.py):DATABASE_URL / SECRET_KEY 环境变量先于 import 设好;
TESTING=True(关 SMTP/log 文件 handler,flask-mail MAIL_SUPPRESS_SEND 落到
testing 缺省即 console 抑制);WTF_CSRF_ENABLED=False(login 表单可直 POST)。
ELASTICSEARCH_URL 留空 → app.elasticsearch=None;REDIS_URL 仅 from_url 惰性
建连,无 redis 进程也能跑。

状态语义(NOTES.md 详):
- SESSION 域:flask-login 会话(@login_required 全家 → 未登录 302);
- DB 域:User/Post/Follow/Message 表,关注关系与私信跨请求守卫;
- 漏洞面:profile 编辑、私信正文、翻译代理、API token。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[4] / "external" / "microblog"

# --- 每进程 sqlite:SF_DB env 或默认临时路径(含 pid)---
SF_DB = os.environ.get("SF_DB") or str(
    Path(tempfile.mkdtemp(prefix=f"microblog-harness-{os.getpid()}-"))
    / "microblog.db"
)
os.environ["DATABASE_URL"] = f"sqlite:///{SF_DB}"     # 先于 import config
os.environ.setdefault("SECRET_KEY", "stateforge-harness-key")

sys.path.insert(0, str(REPO))       # app/ 与 config.py 在仓库根
os.chdir(REPO)                      # 相对路径配置(migrations/logs)以仓库根解析

from config import Config           # noqa: E402


class HarnessConfig(Config):
    """测试配置:每进程 sqlite、无 CSRF、无外部服务。"""

    SQLALCHEMY_DATABASE_URI = f"sqlite:///{SF_DB}"
    TESTING = True                  # 关 mail/log handler;flask-mail 抑制发送
    WTF_CSRF_ENABLED = False
    ELASTICSEARCH_URL = None
    MAIL_SERVER = None              # console/无 SMTP:不发真邮件
    REDIS_URL = "redis://"          # 仅惰性建连,不触网


from app import create_app, db, models   # noqa: E402

APP = create_app(HarnessConfig)

with APP.app_context():
    db.create_all()
    if models.User.query.filter_by(username="probe").first() is None:
        user = models.User(username="probe", email="probe@example.com")
        user.set_password("probe-pass")
        user2 = models.User(username="peer", email="peer@example.com")
        user2.set_password("peer-pass")
        post = models.Post(body="hello from harness", author=user)
        db.session.add_all([user, user2, post])
        db.session.commit()

CLIENT = APP.test_client()

# rq 队列 stub:launch_task 会 current_app.task_queue.enqueue(...) 真连 redis
# (localhost:6379,无 redis 进程 → ConnectionError 5xx)。fuzzing harness 以
# 内存 fake 顶替,get_id() 语义保持,Task 行照常落库。
class _FakeRQJob:
    def get_id(self) -> str:
        return f"fake-task-{os.getpid()}"


class _FakeTaskQueue:
    def enqueue(self, *args, **kwargs):
        return _FakeRQJob()


APP.task_queue = _FakeTaskQueue()

# 登录引导:test_client 保持 session cookie(probe 用户在上一行 ORM 播种)
_LOGIN_R = CLIENT.post("/auth/login",
                       data={"username": "probe", "password": "probe-pass"})
LOGGED_IN = _LOGIN_R.status_code == 302 and b"login" not in _LOGIN_R.data.lower()

# API token 引导:POST /api/tokens(Basic auth)→ Bearer token(api 蓝图门)
_TOKEN = None
try:
    _tok_r = CLIENT.post("/api/tokens", auth=("probe", "probe-pass"))
    if _tok_r.status_code == 200:
        import json as _json
        _TOKEN = _json.loads(_tok_r.data).get("token")
except Exception:                                           # noqa: BLE001
    _TOKEN = None
_BEARER = {"Authorization": f"Bearer {_TOKEN}"} if _TOKEN else {}

# (method, path[, form[, headers]]) —— 全部来自 url_map dump,参数用种子值填充
ROUTES: list[tuple] = [
    ("GET", "/"),                                  # index(已登录:博客列表)
    ("GET", "/index"),
    ("GET", "/explore"),
    ("GET", "/user/probe"),
    ("GET", "/user/peer"),
    ("GET", "/user/probe/popup"),
    ("GET", "/user/ghost"),                        # 真实模式,无此用户 → 404
    ("GET", "/edit_profile"),
    ("GET", "/messages"),
    ("GET", "/notifications"),
    ("GET", "/search?q=hello"),
    ("GET", "/search?q="),
    ("GET", "/export_posts"),
    ("GET", "/auth/login"),
    ("GET", "/auth/register"),
    ("GET", "/auth/reset_password_request"),
    ("GET", "/api/users", None, _BEARER),          # token 门,已引导
    ("GET", "/api/users/1", None, _BEARER),
    ("GET", "/api/users/1/followers", None, _BEARER),
    ("GET", "/api/users/1/following", None, _BEARER),
    ("GET", "/api/users"),                         # 无 token → 401(守卫展示)
    ("POST", "/follow/peer"),                      # DB 域:关注关系写入
    ("POST", "/unfollow/peer"),
    ("POST", "/send_message/peer", {"message": "hi from harness"}),
    ("POST", "/edit_profile",
     {"username": "probe", "about_me": "harness was here"}),
    ("POST", "/auth/login", {"username": "probe", "password": "wrong"}),
    ("GET", "/nonexistent"),                       # 404 路径
    ("GET", "/auth/logout"),                       # 最后:清会话
]


def smoke() -> list[tuple[str, int | str]]:
    out = []
    for route in ROUTES:
        method, path = route[0], route[1]
        form = route[2] if len(route) > 2 else None
        headers = route[3] if len(route) > 3 else None
        try:
            if method == "GET":
                r = CLIENT.get(path, headers=headers)
            elif form is not None:
                r = CLIENT.post(path, data=form)
            else:
                r = CLIENT.post(path)
            out.append((f"{method} {path}", r.status_code))
        except Exception as exc:                   # noqa: BLE001
            out.append((f"{method} {path}",
                        f"EXC:{type(exc).__name__}:{exc}"))
    return out


if __name__ == "__main__":
    results = smoke()
    ok = sum(1 for _, s in results if isinstance(s, int) and s < 500)
    print(f"smoke: {ok}/{len(results)} non-5xx "
          f"(db={SF_DB}, login={'ok' if LOGGED_IN else 'FAIL'})")
    for route, status in results:
        print(f"  {status!s:>4}  {route}")
