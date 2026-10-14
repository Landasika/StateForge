"""healthchecks 靶 harness(Django 6.1 + sqlite,批 1)。

启动:每进程独立 sqlite(DB_NAME 环境变量,settings.py 原生支持)
→ migrate → ORM 引导(user/project/check/api key,镜像 _make_user)
→ django.test.Client(会话 cookie 跨请求持久 = 跨请求状态载体)。

状态语义(NOTES.md 详):
- 会话域:hc.front 全家 @login_required;
- DB 域:Check.status 生命周期 new→up/down(/ping/<code> 按 code 定位,
  无认证 capability-token 状态)、pause/resume、API key 门。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4] / "external" / "healthchecks"

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hc.settings")
os.environ.setdefault("DEBUG", "True")
os.environ.setdefault("ALLOWED_HOSTS", "testserver,localhost")
_DB = Path(os.environ.get("SF_DB", REPO / f".sf_db_{os.getpid()}.sqlite3"))
os.environ["DB_NAME"] = str(_DB)

sys.path.insert(0, str(REPO))

import django  # noqa: E402

django.setup()

from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402

call_command("migrate", verbosity=0, interactive=False, run_syncdb=True)

# ---- 引导:user + project + first check + api key(镜像 views._make_user) ----
from hc.accounts.models import Profile, Project, User  # noqa: E402
from hc.api.models import Check  # noqa: E402

_USER, _PASS = "probe@example.org", "passw0rd!"
user = User(username="probe", email=_USER)
user.set_password(_PASS)
user.save()
Profile.objects.for_user(user)                    # check_limit 放开
project = Project(owner=user)
project.badge_key = user.username
project.save()
check = Check(project=project)
check.name = "Probe Check"
check.slug = "probe-check"
check.save()
_rw = _ro = None
try:
    _rw = project.set_api_key()                    # 明文仅此一次(存哈希)
    _ro = project.set_api_key_readonly()
    project.save()
except Exception:
    pass

CLIENT = Client()
CLIENT.login(username=_USER, password=_PASS)

CODE = str(check.code)
PROJECT_CODE = str(project.code)
API_KEY = _rw or ""

# ---- 路由表(冒烟 + 种子参数池共用;auth 标注状态域) ----
ROUTES: list[tuple[str, str, str]] = [
    ("GET", "/", "public"),
    ("GET", "/docs/", "public"),
    ("GET", "/docs/cron/", "public"),
    ("GET", "/accounts/login/", "public"),
    ("POST", "/accounts/login/", "auth-form"),
    ("GET", "/tv/", "session"),
    ("GET", "/projects/menu/", "session"),
    ("GET", f"/projects/{PROJECT_CODE}/checks/", "session"),
    ("POST", f"/projects/{PROJECT_CODE}/checks/add/", "session"),
    ("GET", f"/projects/{PROJECT_CODE}/integrations/", "session"),
    ("GET", "/accounts/profile/", "session"),
    ("GET", f"/projects/{PROJECT_CODE}/settings/", "session"),
    ("GET", f"/projects/{PROJECT_CODE}/badges/", "session"),
    ("GET", f"/checks/{CODE}/status/", "session"),
    ("GET", f"/checks/{CODE}/details/", "session"),
    # ping 族:Check.status 跨请求状态迁移(无认证,code=capability)
    ("GET", f"/ping/{CODE}", "capability"),
    ("GET", f"/ping/{CODE}/fail", "capability"),
    ("GET", f"/ping/{CODE}/start", "capability"),
    ("GET", f"/ping/{CODE}/log", "capability"),
    ("GET", f"/ping/{CODE}/1", "capability"),
    # API v3:key 门 + 读写状态
    ("GET", "/api/v3/checks/", "api-key"),
    ("POST", "/api/v3/checks/", "api-key"),
    ("GET", f"/api/v3/checks/{CODE}", "api-key"),
    ("POST", f"/api/v3/checks/{CODE}/pause", "api-key"),
    ("POST", f"/api/v3/checks/{CODE}/resume", "api-key"),
]

_POST_BODIES = {
    "/checks/add/": {"name": "from-harness", "tags": "probe", "kind": "simple",
                     "timeout": 3600, "grace": 60, "tz": "UTC", "schedule": ""},
    "/api/v3/checks/": {"name": "api-check", "slug": "api-check", "timeout": 60},
}


def smoke() -> list[tuple[str, int]]:
    """门 1:路由表冒烟,返回 [(route, status)]。"""
    out = []
    for method, path, auth in ROUTES:
        try:
            headers = {}
            if auth == "api-key" and API_KEY:
                headers["X-Api-Key"] = API_KEY
            extra = {f"HTTP_{k.replace('-', '_').upper()}": v
                     for k, v in headers.items()}
            body = None
            if method == "POST":
                body = next((v for k, v in _POST_BODIES.items()
                             if path.endswith(k)), None)
            if method == "POST":
                if path.startswith("/api/"):
                    r = CLIENT.post(path, data=json.dumps(body) if body else "",
                                    content_type="application/json", **extra)
                else:                             # 表单路由:form-encoded
                    r = CLIENT.post(path, data=body or {}, **extra)
            else:
                r = CLIENT.get(path, **extra)
            out.append((f"{method} {path}", r.status_code))
        except Exception as exc:                     # noqa: BLE001
            out.append((f"{method} {path}", f"EXC:{type(exc).__name__}"))
    return out


if __name__ == "__main__":
    results = smoke()
    ok = sum(1 for _, s in results if isinstance(s, int) and s < 500)
    print(f"smoke: {ok}/{len(results)} non-5xx")
    for route, status in results:
        print(f"  {status!s:>4}  {route}")
