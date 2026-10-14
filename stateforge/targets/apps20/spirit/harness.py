"""Spirit 靶 harness(Django 论坛,批 2)。

形态:Spirit 是可安装 Django 应用,宿主项目经 `spirit startproject`
脚手架生成 → 每进程在临时目录 scaffold + spiritinstall(migrate)
→ createsuperuser(--noinput)→ Client 会话。

状态语义(NOTES.md 详):
- 会话域:登录门(topic 发表/关注/私信);
- DB 域:论坛分类树→主题→楼层的多级持久结构、置顶/关闭状态。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

VENV_BIN = Path(__file__).resolve().parents[4] / "external" / "Spirit" / ".venv" / "bin"
REPO = VENV_BIN.parent

_SITE = Path(tempfile.mkdtemp(prefix=f"spirit_site_{os.getpid()}_"))
_env = {**os.environ, "PATH": f"{VENV_BIN}:{os.environ.get('PATH', '')}",
        "DJANGO_SUPERUSER_PASSWORD": "Passw0rd!"}
subprocess.run([str(VENV_BIN / "spirit"), "startproject", "spirit_host"],
               cwd=_SITE, env=_env, check=True, capture_output=True)
_PROJ = _SITE / "spirit_host"

os.environ["DJANGO_SETTINGS_MODULE"] = "spirit_host.settings.dev"
os.environ["DJANGO_SUPERUSER_PASSWORD"] = "Passw0rd!"
sys.path.insert(0, str(_PROJ))

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

_DB = Path(os.environ.get("SF_DB", _PROJ / f".sf_db_{os.getpid()}.sqlite"))
settings.DATABASES["default"]["NAME"] = str(_DB)
settings.ALLOWED_HOSTS += ["testserver", "localhost"]

from django.core.management import call_command  # noqa: E402

call_command("migrate", verbosity=0, interactive=False, run_syncdb=True)

_USER, _PASS = "admin", "Passw0rd!"
from django.contrib.auth import get_user_model  # noqa: E402

User = get_user_model()
if not User.objects.filter(username=_USER).exists():
    User.objects.create_superuser(_USER, "admin@spirit.local", _PASS)

from django.test import Client  # noqa: E402

CLIENT = Client()
CLIENT.login(username=_USER, password=_PASS)

ROUTES: list[tuple[str, str]] = [
    ("GET", "/"),
    ("GET", "/category/"),
    ("GET", "/search/?q=probe"),
    ("GET", "/topic/active/"),
    ("GET", "/topic/unread/"),
    ("GET", "/topic/notification/"),
    ("GET", "/comment/upload/"),
    ("GET", "/user/"),
    ("GET", "/user/login/"),
    ("GET", "/user/register/"),
    ("GET", "/user/menu/"),
    ("GET", "/user/password-reset/"),
    ("GET", "/user/resend-activation/"),
    ("GET", "/user/topics/1/"),
    ("GET", "/user/likes/1/"),
    ("GET", "/st/admin/dashboard/"),
    ("GET", "/st/admin/category/"),
    ("GET", "/st/admin/topic/"),
    ("GET", "/st/admin/topic/closed/"),
    ("GET", "/st/admin/topic/pinned/"),
    ("GET", "/st/admin/user/"),
    ("GET", "/st/admin/user/admins/"),
    ("GET", "/st/admin/user/mods/"),
    ("GET", "/st/admin/config/"),
]


def smoke() -> list[tuple[str, int]]:
    out = []
    for method, path in ROUTES:
        try:
            r = CLIENT.get(path) if method == "GET" else CLIENT.post(path)
            out.append((f"{method} {path}", r.status_code))
        except Exception as exc:                 # noqa: BLE001
            out.append((f"{method} {path}", f"EXC:{type(exc).__name__}"))
    return out


if __name__ == "__main__":
    results = smoke()
    ok = sum(1 for _, s in results if isinstance(s, int) and s < 500)
    print(f"smoke: {ok}/{len(results)} non-5xx site={_PROJ}")
    for route, status in results:
        print(f"  {status!s:>4}  {route}")
