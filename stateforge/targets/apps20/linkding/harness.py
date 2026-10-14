"""linkding 靶 harness(书签管理,Django 6.1 + sqlite,批 2)。

启动:每进程独立 sqlite(BOOT 后覆写 settings.DATABASES["default"]["NAME"])
→ migrate → ORM 引导(superuser + bookmark/tag/bundle/feed token/api token 种子)
→ django.test.Client(会话 cookie 跨请求持久 = 跨请求状态载体)。

坑位记录:
- huey.contrib.djhuey 在 django.setup() 时即打开 data/tasks.sqlite3,
  须先 mkdir REPO/data,否则 sqlite3.OperationalError: unable to open database file;
- LD_DISABLE_BACKGROUND_TASKS=True 关掉 favicon/预取/snapshot 入队(任务执行依赖
  worker,且 favicon provider 会发外部 HTTP);
- api/bookmarks/check 会经 website_loader 真发 HTTP,冒烟路由表避开;
- Tag.date_added / Bookmark.date_added|date_modified 无默认值,种子须显式填。

状态语义(NOTES.md 详):
- 会话域:bookmarks.views.* @login_required 全家;
- DB 域:Bookmark 存在性(edit/archive 系)、FeedToken.key capability(feeds 族,
  无认证)、ApiToken(LinkdingTokenAuthentication)。
"""

from __future__ import annotations

import datetime
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4] / "external" / "linkding"

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "bookmarks.settings")
os.environ.setdefault("LD_DISABLE_BACKGROUND_TASKS", "True")  # 不入队 favicon/预取任务
_DB = Path(os.environ.get("SF_DB", REPO / f".sf_db_{os.getpid()}.sqlite3"))
os.makedirs(REPO / "data", exist_ok=True)  # huey SqliteHuey 在 setup() 时建表

sys.path.insert(0, str(REPO))

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

settings.DATABASES["default"]["NAME"] = str(_DB)
settings.ALLOWED_HOSTS = list(settings.ALLOWED_HOSTS) + ["testserver", "localhost"]

from django.core.management import call_command  # noqa: E402

call_command("migrate", verbosity=0, interactive=False, run_syncdb=True)

# ---- 引导:superuser + 书签/标签/合集/令牌种子 ----
from django.contrib.auth import get_user_model  # noqa: E402

_USER, _PASS = "probe", "Passw0rd!"
User = get_user_model()
if not User.objects.filter(username=_USER).exists():
    User.objects.create_superuser(_USER, "probe@linkding.local", _PASS)

from bookmarks.models import (  # noqa: E402
    ApiToken,
    Bookmark,
    BookmarkBundle,
    FeedToken,
    Tag,
)

_NOW = datetime.datetime.now(datetime.timezone.utc)
_owner = User.objects.get(username=_USER)
_tag = Tag.objects.filter(owner=_owner, name="probe-tag").first()
if _tag is None:
    _tag = Tag.objects.create(name="probe-tag", owner=_owner, date_added=_NOW)
_bm = Bookmark.objects.filter(owner=_owner, url="https://example.com/probe").first()
if _bm is None:
    _bm = Bookmark(
        url="https://example.com/probe",
        title="Probe Bookmark",
        description="probe",
        unread=True,
        shared=True,
        date_added=_NOW,
        date_modified=_NOW,
        owner=_owner,
    )
    _bm.save()
    _bm.tags.add(_tag)
_bm_archived = Bookmark.objects.filter(owner=_owner, url="https://example.com/archived").first()
if _bm_archived is None:
    _bm_archived = Bookmark(
        url="https://example.com/archived",
        title="Archived Probe",
        is_archived=True,
        date_added=_NOW,
        date_modified=_NOW,
        owner=_owner,
    )
    _bm_archived.save()
_bundle = BookmarkBundle.objects.filter(owner=_owner, name="Probe Bundle").first()
if _bundle is None:
    _bundle = BookmarkBundle.objects.create(name="Probe Bundle", owner=_owner)
_feed = FeedToken.objects.filter(user=_owner).first()
if _feed is None:
    _feed = FeedToken.objects.create(user=_owner)
_api_token = ApiToken.objects.filter(user=_owner, name="probe-token").first()
if _api_token is None:
    _api_token = ApiToken.objects.create(user=_owner, name="probe-token")

from django.test import Client  # noqa: E402

CLIENT = Client()
CLIENT.login(username=_USER, password=_PASS)

BM_ID = _bm.pk
TAG_ID = _tag.pk
BUNDLE_ID = _bundle.pk
FEED_KEY = _feed.key
API_TOKEN = _api_token.key

# ---- 路由表(冒烟 + 种子参数池共用;第三列标注状态域) ----
ROUTES: list[tuple[str, str, str]] = [
    ("GET", "/", "public"),                      # root:按登录态 302
    ("GET", "/login/", "public"),
    # 会话域:bookmarks 前端全家 @login_required
    ("GET", "/bookmarks", "session"),
    ("GET", "/bookmarks/archived", "session"),
    ("GET", "/bookmarks/shared", "session"),
    ("GET", "/bookmarks/new", "session"),
    ("GET", f"/bookmarks/{BM_ID}/edit", "session+db"),  # Bookmark 存在性
    ("GET", "/tags", "session"),
    ("GET", "/tags/new", "session"),
    ("GET", f"/tags/{TAG_ID}/edit", "session+db"),     # Tag 存在性
    ("GET", "/tags/merge", "session"),
    ("GET", "/bundles", "session"),
    ("GET", "/bundles/new", "session"),
    ("GET", f"/bundles/{BUNDLE_ID}/edit", "session+db"),
    ("GET", "/bundles/preview", "session"),
    ("GET", "/settings", "session"),
    ("GET", "/settings/general", "session"),
    ("GET", "/settings/integrations", "session"),
    ("GET", "/settings/import", "session"),
    ("GET", "/settings/export", "session"),
    ("GET", "/admin/tasks/", "session"),
    ("GET", "/admin/", "session"),
    # capability 域:FeedToken.key 无认证 RSS(feeds.py::get_object 按 key 定位)
    ("GET", f"/feeds/{FEED_KEY}/all", "capability"),
    ("GET", f"/feeds/{FEED_KEY}/unread", "capability"),
    ("GET", f"/feeds/{FEED_KEY}/shared", "capability"),
    ("GET", "/feeds/shared", "public"),
    # api-token 域:LinkdingTokenAuthentication / SessionAuthentication
    ("GET", "/api/", "api-token"),
    ("GET", "/api/bookmarks/", "api-token"),
    ("GET", "/api/bookmarks/archived/", "api-token"),
    ("GET", f"/api/bookmarks/{BM_ID}/", "api-token+db"),
    ("GET", "/api/tags/", "api-token"),
    ("GET", "/api/tags/?limit=5", "api-token"),
    ("GET", "/api/bundles/", "api-token"),
    ("GET", "/api/user/profile/", "api-token"),
    # 杂项 public
    ("GET", "/health", "public"),
    ("GET", "/manifest.json", "public"),
    ("GET", "/custom_css", "public"),
    ("GET", "/opensearch.xml", "public"),
    ("GET", "/assets/1", "session+db"),           # 无 asset 种子 → 404(守卫正常工作)
]


def smoke() -> list[tuple[str, int]]:
    """门 1:路由表冒烟,返回 [(route, status)]。"""
    out = []
    for method, path, auth in ROUTES:
        try:
            headers = {}
            if auth.startswith("api-token"):
                headers["Authorization"] = f"Token {API_TOKEN}"
            extra = {f"HTTP_{k.replace('-', '_').upper()}": v
                     for k, v in headers.items()}
            r = CLIENT.get(path, **extra) if method == "GET" else CLIENT.post(path, **extra)
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
