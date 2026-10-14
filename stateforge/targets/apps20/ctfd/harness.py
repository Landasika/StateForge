"""CTFd 靶 harness(Flask 3 + SQLAlchemy,批 2)。

启动:DATABASE_URL 环境变量 → 每进程 sqlite → create_app →
app.db.create_all() → ORM 建 admin → session_transaction 登录。

状态语义(NOTES.md 详):
- 会话域:CTFd 自管 session auth(id+hash);
- DB 域:竞赛生命周期(setup→challenges→submissions→scoreboard),
  冻结/可见性/队伍状态;
- 复合:admin 门(admin 配置页全家)。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4] / "external" / "CTFd"

sys.path.insert(0, str(REPO))
os.environ.setdefault("SECRET_KEY", "sf-probe-secret")
_DB = Path(os.environ.get("SF_DB", REPO / f".sf_db_{os.getpid()}.sqlite"))
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
# SECRET_KEY_FILE 默认写 CWD(.ctfd_secret_key)→ 改道每进程临时路径
os.environ.setdefault("SECRET_KEY_FILE",
                      str(REPO / f".sf_secret_{os.getpid()}"))

from CTFd import create_app  # noqa: E402

APP = create_app()

with APP.app_context():
    APP.db.create_all()
    from CTFd.models import Users, Configs

    if not Configs.query.filter_by(key="setup").first():
        from CTFd.utils import set_config

        set_config("setup", "CTFd-probe")     # 经 set_config 使缓存失效
    _USER, _PASS = "probe", "Passw0rd!"
    _u = Users.query.filter_by(name=_USER).first()
    if not _u:
        admin = Users(name=_USER, email="probe@ctfd.local",
                      password=_PASS, type="admin", hidden=False,
                      banned=False, verified=True)
        APP.db.session.add(admin)
        APP.db.session.commit()
        _u = admin
    from CTFd.utils.security.signing import hmac as ctfd_hmac

    _UID = _u.id

CLIENT = APP.test_client()
with CLIENT.session_transaction() as sess:
    sess["id"] = _UID
    sess["name"] = _USER
    sess["type"] = "admin"
    sess["hash"] = ctfd_hmac(_u.password)

ROUTES: list[tuple[str, str, str]] = [
    ("GET", "/", "public"),                        # SPA 入口
    ("GET", "/login", "public"),
    ("GET", "/register", "public"),
    ("GET", "/challenges", "session"),
    ("GET", "/scoreboard", "session"),
    ("GET", "/notifications", "session"),
    ("GET", "/settings", "session"),
    ("GET", "/users", "session"),
    ("GET", "/teams", "session"),
    ("GET", "/setup", "public"),
    ("GET", "/tos", "public"),
    ("GET", "/privacy", "public"),
    ("GET", "/admin", "admin"),
    ("GET", "/admin/challenges", "admin"),
    ("GET", "/admin/challenges/new", "admin"),
    ("GET", "/admin/config", "admin"),
    ("GET", "/admin/statistics", "admin"),
    ("GET", "/admin/scoreboard", "admin"),
    ("GET", "/admin/submissions", "admin"),
    ("GET", "/admin/pages", "admin"),
    ("GET", "/admin/pages/new", "admin"),
    ("GET", "/admin/reset", "admin"),
    ("GET", "/api/v1/users", "session"),
    ("GET", "/api/v1/users/me", "session"),
    ("GET", "/api/v1/challenges", "session"),
    ("GET", "/api/v1/configs", "session"),
    ("GET", "/api/v1/statistics/challenges/solves", "session"),
    ("GET", "/api/v1/scoreboard", "public"),
]


def smoke() -> list[tuple[str, int]]:
    out = []
    for method, path, auth in ROUTES:
        try:
            r = CLIENT.get(path) if method == "GET" else CLIENT.post(path)
            out.append((f"{method} {path}", r.status_code))
        except Exception as exc:                 # noqa: BLE001
            out.append((f"{method} {path}", f"EXC:{type(exc).__name__}"))
    return out


if __name__ == "__main__":
    results = smoke()
    ok = sum(1 for _, s in results if isinstance(s, int) and s < 500)
    print(f"smoke: {ok}/{len(results)} non-5xx")
    for route, status in results:
        print(f"  {status!s:>4}  {route}")
