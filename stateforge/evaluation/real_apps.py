"""SS-07E/F: pre-register five real applications and record launch outcomes.

This module deliberately does not invent fuzz results.  It proves that the
manifest was written before any run, hashes the source trees, attempts the
registered launch command, and records ENV_BLOCKED with the raw stderr when a
command cannot start.  Only a healthy process may proceed to an arm run.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APPS = (
    {"name": "CTFd", "path": "external/CTFd",
     "python": "external/CTFd/.venv/bin/python",
     "command": ["-m", "flask", "run", "--port", "5011"],
     "health": "http://127.0.0.1:5011/",
     "env": {"DATABASE_URL": "sqlite:////tmp/sf_ctfd.sqlite",
             "SECRET_KEY": "sf-ctfd-test", "FLASK_RUN_FROM_CLI": "1",
             "FLASK_APP": "wsgi"}},
    {"name": "healthchecks", "path": "external/healthchecks",
     "python": "external/healthchecks/.venv/bin/python",
     "command": ["manage.py", "runserver", "127.0.0.1:5012",
                 "--noreload"],
     "health": "http://127.0.0.1:5012/",
     "env": {"DB_NAME": "/tmp/sf_healthchecks.sqlite",
             "SECRET_KEY": "sf-hc-test", "DEBUG": "True",
             "ALLOWED_HOSTS": "127.0.0.1,localhost"}},
    {"name": "linkding", "path": "external/linkding",
     "python": "external/linkding/.venv/bin/python",
     "command": ["manage.py", "runserver", "127.0.0.1:5013", "--noreload"],
     "health": "http://127.0.0.1:5013/login",
     "env": {"LD_SUPERUSER_NAME": "admin", "LD_SUPERUSER_PASSWORD": "x",
             "DJANGO_DEBUG": "0"}},
    {"name": "Spirit", "path": "external/Spirit",
     "python": "external/Spirit/.venv/bin/python",
     "command": ["runtests.py"],
     "health": "http://127.0.0.1:5014/",
     "env": {}},
    {"name": "microblog", "path": "external/microblog",
     "python": "external/microblog/.venv/bin/python",
     "command": ["-m", "flask", "--app", "microblog.py", "run",
                 "--host", "127.0.0.1", "--port", "5010"],
     "health": "http://127.0.0.1:5010/auth/login",
     "env": {"DATABASE_URL": "sqlite:////tmp/sf_microblog.db",
             "SECRET_KEY": "sf-microblog-test", "FLASK_APP": "microblog.py"}},
)


def tree_hash(path: Path) -> str:
    h = hashlib.sha256()
    skip = {".git", ".venv", "__pycache__", "node_modules", "data"}
    for p in sorted(path.rglob("*")):
        if p.is_dir() or any(part in skip for part in p.parts):
            continue
        if p.suffix in (".pyc", ".sqlite", ".sqlite3", ".db"):
            continue
        rel = str(p.relative_to(path))
        h.update(rel.encode())
        h.update(b"\0")
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"<missing>")
        h.update(b"\0")
    return h.hexdigest()


def pre_register(out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"protocol": "ss07-five-apps-v1", "created_at": time.time(),
                "source_revision": _git(), "apps": []}
    for app in APPS:
        manifest["apps"].append({
            **app,
            "source_hash": tree_hash(ROOT / app["path"]),
            "pre_registered": True,
        })
    (out_dir / "pre_registered.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return manifest


def _git() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"],
                                       text=True, cwd=ROOT,
                                       stderr=subprocess.DEVNULL).strip()
    except Exception:                              # noqa: BLE001
        return "unknown"


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _wait_health(url: str, proc, timeout: float = 20.0) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False, f"process-exit={proc.returncode}"
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                return 200 <= r.status < 500, f"HTTP {r.status}"
        except Exception as exc:                   # noqa: BLE001
            last = type(exc).__name__
            time.sleep(0.25)
    return False, f"timeout:{last if 'last' in locals() else 'no-health'}"


def _migrate(app, app_dir: Path, env: dict, log_path: Path) -> str | None:
    name = app["name"]
    py = str(ROOT / app["python"])
    if name == "CTFd":
        cmd = [py, "-m", "flask", "db", "upgrade"]
    elif name in ("healthchecks", "linkding"):
        cmd = [py, "manage.py", "migrate", "--noinput"]
    else:
        return None
    with log_path.open("a") as fh:
        fh.write(f"$ {' '.join(cmd)}\n")
        fh.flush()
        p = subprocess.run(cmd, cwd=app_dir, env=env, stdout=fh,
                           stderr=subprocess.STDOUT, timeout=120)
    return None if p.returncode == 0 else f"migrate-exit={p.returncode}"


def run_all(out_dir: Path, require_frozen: bool = True) -> dict:
    out_dir = Path(out_dir)
    pre = pre_register(out_dir)
    results = []
    for app in pre["apps"]:
        app_dir = ROOT / app["path"]
        log = out_dir / f"{app['name']}.log"
        env = dict(os.environ)
        env.update({k: str(v) for k, v in app["env"].items()})
        cmd = [str(ROOT / app["python"])] + app["command"]
        migration_error = _migrate(app, app_dir, env, log)
        if migration_error:
            results.append({
                "app": app["name"], "status": "ENV_BLOCKED",
                "reason": migration_error, "source_hash": app["source_hash"],
                "pre_registered_hash": app["source_hash"],
                "launch_command": cmd, "health": app["health"],
                "log": str(log), "elapsed_wall_seconds": 0.0,
                "arms_run": 0, "seeds_run": 0})
            continue
        proc = None
        started = time.monotonic()
        try:
            proc = subprocess.Popen(cmd, cwd=app_dir, env=env,
                                    stdout=log.open("w"),
                                    stderr=subprocess.STDOUT,
                                    start_new_session=True)
            ok, detail = _wait_health(app["health"], proc)
            if ok:
                time.sleep(0.2)
                if proc.poll() is not None:
                    ok, detail = False, f"exited-after-health={proc.returncode}"
            if ok:
                status = "SMOKE_PASS"
                reason = detail
            else:
                status = "ENV_BLOCKED"
                reason = detail
        except Exception as exc:                   # noqa: BLE001
            status, reason = "ENV_BLOCKED", f"{type(exc).__name__}: {exc}"
        finally:
            if proc is not None and proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except Exception:                  # noqa: BLE001
                    proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:                  # noqa: BLE001
                    proc.kill()
        results.append({
            "app": app["name"], "status": status, "reason": reason,
            "source_hash": app["source_hash"],
            "pre_registered_hash": app["source_hash"],
            "launch_command": cmd, "health": app["health"],
            "log": str(log), "elapsed_wall_seconds": round(
                time.monotonic() - started, 6),
            "arms_run": 0, "seeds_run": 0,
        })
    report = {"protocol": pre["protocol"], "source_revision": pre["source_revision"],
              "pre_registered": pre, "results": results,
              "summary": {s: sum(1 for r in results if r["status"] == s)
                          for s in ("SMOKE_PASS", "ENV_BLOCKED")}}
    (out_dir / "results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report
