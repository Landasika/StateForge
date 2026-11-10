"""CTFd join 驱动:每请求独立追踪 + 前后快照 + executed 行集。

产出 /tmp/sf_join_steps.json:
  steps: [{trigger, label, status, before, after, executed: [[file,line],..]}]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, "/tmp")
from sf_trace_collect import load_tracer           # noqa: E402

REPO = Path("/home/landasika/icassp/external/CTFd").resolve()
sys.path.insert(0, str(REPO))
import os                                           # noqa: E402
os.environ.setdefault("SECRET_KEY", "sf-probe-secret")
os.environ["DATABASE_URL"] = f"sqlite:///{REPO / f'.sf_probe_join_{os.getpid()}.sqlite'}"
os.environ.setdefault("SECRET_KEY_FILE",
                      str(REPO / f".sf_secret_join_{os.getpid()}"))
OUT = Path("/tmp/sf_join_steps.json")


def _snap(client):
    with client.session_transaction() as s:
        return {k: (v if isinstance(v, (str, int, float, bool, type(None)))
                    else repr(v)) for k, v in s.items()}


def _nonce(client):
    with client.session_transaction() as s:
        return s.get("nonce")


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


def main():
    tracer = load_tracer()
    client = setup()
    steps = [
        ("GET /", "GET", lambda: client.get("/")),
        ("GET /login", "GET", lambda: client.get("/login")),
        ("POST /login", "POST", lambda: client.post("/login", data={
            "name": "admin", "password": "nope", "nonce": _nonce(client)})),
        ("POST /login", "POST", lambda: client.post("/login", data={
            "name": "admin", "password": "ProbePass1!",
            "nonce": _nonce(client)})),
        ("GET /challenges", "GET", lambda: client.get("/challenges")),
        ("GET /admin/config", "GET", lambda: client.get("/admin/config")),
        ("GET /logout", "GET", lambda: client.get("/logout")),
        ("GET /challenges", "GET", lambda: client.get("/challenges")),
    ]
    rows = []
    for label, method, fn in steps:
        before = _snap(client)
        with tracer.TraceSession(REPO) as trace:
            resp = fn()
        after = _snap(client)
        executed = sorted({(e.file, e.line) for e in trace.events})
        rows.append({
            "trigger": f"{method} {label.split(' ', 1)[1]}",
            "label": label, "status": resp.status_code,
            "before": before, "after": after,
            "executed": [[f, ln] for f, ln in executed]})
        print(f"{resp.status_code} {label}")
    OUT.write_text(json.dumps(rows), encoding="utf-8")
    print("steps:", len(rows))


if __name__ == "__main__":
    main()
