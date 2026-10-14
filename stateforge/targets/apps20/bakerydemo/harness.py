"""bakerydemo 靶 harness(Wagtail 8 CMS demo,Django 6 + sqlite,批 1)。

启动:每进程独立 sqlite(DATABASE_NAME 环境变量)→ migrate
→ ORM 引导(superuser + StandardIndexPage/StandardPage 子树)
→ Client(wagtail admin 会话)。

状态语义(NOTES.md 详):
- 会话域:wagtail admin @login_required(/admin/ 全家);
- DB 域:页面树(site root → index → page,按 slug 定位路由)、
  search 索引、documents 所有权;
- API 域:/api/v2/pages/ 公开,发布状态(draft/live)门深块。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4] / "external" / "bakerydemo"

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "bakerydemo.settings.dev")
_DB = Path(os.environ.get("SF_DB", REPO / f".sf_db_{os.getpid()}.sqlite3"))
os.environ["DATABASE_NAME"] = str(_DB)
os.environ["DJANGO_DEBUG"] = "1"

sys.path.insert(0, str(REPO))
os.chdir(REPO)          # TEMPLATES["DIRS"] 为相对路径("bakerydemo/templates")

import django  # noqa: E402

django.setup()

from django.core.management import call_command  # noqa: E402

call_command("migrate", verbosity=0, interactive=False, run_syncdb=True)

# ---- 引导:superuser + 探针页面树 ----
from django.contrib.auth import get_user_model  # noqa: E402
from wagtail.models import Page  # noqa: E402

_USER, _PASS = "admin", "passw0rd!"
User = get_user_model()
if not User.objects.filter(username=_USER).exists():
    User.objects.create_superuser(_USER, "admin@example.org", _PASS)

from bakerydemo.base.models import StandardPage  # noqa: E402

home = Page.objects.filter(depth=2).first()          # wagtail welcome 首页
_idx = _page = None
if home is not None:
    try:
        idx = StandardPage(title="Probe", slug="probe",
                           introduction="probe intro")
        home.add_child(instance=idx)
        page = StandardPage(title="Deep Page", slug="deep",
                            introduction="deep page intro")
        idx.add_child(instance=page)
        _idx, _page = idx, page
    except Exception:
        pass

CLIENT = None
from django.test import Client  # noqa: E402

CLIENT = Client()
CLIENT.login(username=_USER, password=_PASS)

# ---- 路由表(冒烟 + 种子参数池;auth 标注状态域) ----
ROUTES: list[tuple[str, str, str]] = [
    ("GET", "/", "public"),                          # wagtail welcome 页
    ("GET", "/search/?q=bread", "public"),
    ("GET", "/sitemap.xml", "public"),
    ("GET", "/api/v2/pages/", "public"),
    ("GET", "/api/v2/pages/?type=bakerydemo.base.StandardPage", "public"),
    ("GET", "/admin/login/", "public"),
    ("GET", "/admin/", "session"),                   # wagtail admin home
    ("GET", "/admin/pages/", "session"),
    ("GET", "/admin/users/", "session"),
    ("GET", "/admin/documents/", "session"),
    ("GET", "/admin/images/", "session"),
    ("GET", "/admin/snippets/", "session"),
    ("GET", "/admin/forms/", "session"),
    ("GET", "/django-admin/", "session"),
    ("GET", "/documents/", "public"),
    ("GET", "/404/", "public"),
]
if _idx is not None:
    ROUTES += [
        ("GET", "/probe/", "page-tree"),             # 页面树按 slug 路由
        ("GET", "/probe/deep/", "page-tree"),
        ("GET", f"/api/v2/pages/{_page.id}/", "public"),
        ("GET", "/admin/pages/%d/edit/" % _page.id, "session"),
    ]


def smoke() -> list[tuple[str, int]]:
    """门 1:路由表冒烟。"""
    out = []
    for method, path, auth in ROUTES:
        try:
            if method == "GET":
                r = CLIENT.get(path)
            else:
                r = CLIENT.post(path, data=json.dumps({}),
                                content_type="application/json")
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
