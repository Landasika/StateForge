"""CTFd session-transition driver:真实请求流 + 每请求前后 session 快照。

流程设计(每步都是一次真实 HTTP 包):
  1 GET /                匿名首页
  2 GET /login           登录页
  3 POST /login 错密码    失败的转移尝试(状态应不变)
  4 POST /login 正确      转移点应执行(auth.py 的 session 写)
  5 GET /challenges      守卫 authed() 应通过
  6 GET /admin/config    管理员守卫
  7 POST /logout         转移点应执行(session.clear)
  8 GET /challenges      守卫应再次失败
"""
import json
import os
import sys
from pathlib import Path

REPO = Path("/home/landasika/icassp/external/CTFd").resolve()
sys.path.insert(0, str(REPO))
os.environ.setdefault("SECRET_KEY", "sf-probe-secret")
_DB = REPO / f".sf_probe_sess_{os.getpid()}.sqlite"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("SECRET_KEY_FILE", str(REPO / f".sf_secret_sess_{os.getpid()}"))
OUT = Path("/tmp/sf_sess_requests.json")


def _snap(client):
    try:
        with client.session_transaction() as s:
            return {k: (v if isinstance(v, (str, int, float, bool, type(None)))
                        else repr(v))
                    for k, v in s.items()}
    except Exception as exc:                    # noqa: BLE001
        return {"<error>": f"{type(exc).__name__}: {exc}"}


def setup():
    from CTFd import create_app
    app = create_app()
    with app.app_context():
        from CTFd.models import Configs, Users
        app.db.create_all()
        from CTFd.utils import set_config
        if not Configs.query.filter_by(key="setup").first():
            set_config("setup", "CTFd-probe")
        if Users.query.filter_by(name="admin").first() is None:
            app.db.session.add(Users(
                name="admin", email="admin@example.com",
                password="ProbePass1!", type="admin",
                verified=True, hidden=False))
            app.db.session.commit()
    return app.test_client()


def _nonce(client):
    with client.session_transaction() as s:
        return s.get("nonce")


def run(client):
    steps = [
        ("GET /",             lambda: client.get("/")),
        ("GET /login",        lambda: client.get("/login")),
        ("POST /login 错密码", lambda: client.post("/login", data={
            "name": "admin", "password": "nope", "nonce": _nonce(client)})),
        ("POST /login 正确",  lambda: client.post("/login", data={
            "name": "admin", "password": "ProbePass1!", "nonce": _nonce(client)})),
        ("GET /challenges",   lambda: client.get("/challenges")),
        ("GET /admin/config", lambda: client.get("/admin/config")),
        ("GET /logout",       lambda: client.get("/logout")),
        ("GET /challenges",   lambda: client.get("/challenges")),
    ]
    rows = []
    for label, fn in steps:
        before = _snap(client)
        try:
            resp = fn()
            status = resp.status_code
        except Exception as exc:                # noqa: BLE001
            status = f"ERR {type(exc).__name__}"
        after = _snap(client)
        rows.append({"label": label, "status": status,
                     "before": before, "after": after})
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print("requests recorded:", len(rows))
