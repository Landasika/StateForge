"""django-blog-zinnia 靶 harness(Django 2.2 LTS + sqlite,批 3)。

形态:Zinnia 是可安装 Django 应用库(0.20.1.dev0,项目止于 Django 2.x 时代),
官方 `demo/` 是完整宿主示例。harness 每进程在临时目录生成宿主
settings/urls(仿 demo/settings.py,按 /blog/ 前缀挂 zinnia.urls)
→ migrate → superuser + Entry/Category/Tag 种子 → Client 会话。

依赖版本(venv 实况,py3.13):
- Django==2.2.28(zinnia 模板 `{% load staticfiles %}`/`url()`/
  `Signal(providing_args)` 均为 Django 3.0/4.0 移除项,3.2 也不够)
- setuptools 84(distutils shim:Django 2.2 导入 distutils.version,
  py3.12+ 由 setuptools 的 _distutils_hack 提供)
- legacy-cgi(Django 2.2 http 栈 import cgi,PEP 594 后由 PyPI 顶替)
- django-mptt==0.12.0 / django-contrib-comments==2.0.0(Django 2.2 档)
- 剔除 django_xmlrpc:0.1.8 `from collections import Callable`
  py3.10+ 语法级死亡且无新版;仅 metaweblog/pingback 可选功能需要,
  不进 INSTALLED_APPS 即不影响核心(记录于 NOTES.md)

状态语义(NOTES.md 详):
- 会话域:/blog/quick-entry/ staff 门、django_comments 审核链;
- DB 域:Entry 发布状态(status=2)驱动全部归档/列表/feed/sitemap 路由,
  分类树(Category mptt)+ 标签(tagging)跨请求派生。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[4] / "external" / "django-blog-zinnia"

# ---- 每进程宿主脚手架:临时目录写 settings/urls(仿 demo/) ----
_SITE = Path(tempfile.mkdtemp(prefix=f"zinn_site_{os.getpid()}_"))

_HOST_SETTINGS = '''\
"""Sf host settings for django-blog-zinnia (modeled on demo/settings.py)."""
SECRET_KEY = "sf-zinnia-harness-key"
DEBUG = False
ALLOWED_HOSTS = ["*"]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3",
                         "NAME": ""}}  # harness 导入后按 SF_DB 覆写
TIME_ZONE = "UTC"
USE_TZ = True
USE_I18N = True
SITE_ID = 1
LANGUAGE_CODE = "en"
STATIC_URL = "/static/"
MEDIA_URL = "/media/"
ROOT_URLCONF = "sf_zinnia_urls"
MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.contrib.auth.context_processors.auth",
                "django.template.context_processors.request",
                "django.contrib.messages.context_processors.messages",
                "zinnia.context_processors.version",
            ]
        },
    }
]
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.sites",
    "django.contrib.messages",
    "django.contrib.sitemaps",
    "django.contrib.admin",
    "django.contrib.staticfiles",
    "django_comments",
    "mptt",
    "tagging",
    "zinnia",
]
ZINNIA_PAGINATION = 3
'''

_HOST_URLS = '''\
from django.conf.urls import include, url
from django.contrib import admin
from django.contrib.sitemaps.views import index, sitemap

from zinnia.sitemaps import AuthorSitemap, CategorySitemap, EntrySitemap, TagSitemap

sitemaps = {"tags": TagSitemap, "blog": EntrySitemap,
            "authors": AuthorSitemap, "categories": CategorySitemap}

urlpatterns = [
    url(r"^blog/", include("zinnia.urls", namespace="zinnia")),
    url(r"^comments/", include("django_comments.urls")),
    url(r"^admin/", admin.site.urls),
    url(r"^sitemap\\.xml$", index, {"sitemaps": sitemaps}),
    url(r"^sitemap-(?P<section>.+)\\.xml$", sitemap, {"sitemaps": sitemaps},
        name="django.contrib.sitemaps.views.sitemap"),
]
'''

(_SITE / "sf_zinnia_settings.py").write_text(_HOST_SETTINGS)
(_SITE / "sf_zinnia_urls.py").write_text(_HOST_URLS)

os.environ["DJANGO_SETTINGS_MODULE"] = "sf_zinnia_settings"
sys.path.insert(0, str(_SITE))
sys.path.insert(0, str(REPO))

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

_DB = Path(os.environ.get("SF_DB", _SITE / f".sf_db_{os.getpid()}.sqlite3"))
settings.DATABASES["default"]["NAME"] = str(_DB)

from django.core.management import call_command  # noqa: E402

# Django 2.2 下 zinnia/tagging/django_comments 迁移全部直接通过,无需 --fake
call_command("migrate", verbosity=0, interactive=False, run_syncdb=True)

_USER, _PASS = "probe", "Passw0rd!"
from django.contrib.auth import get_user_model  # noqa: E402

User = get_user_model()
if not User.objects.filter(username=_USER).exists():
    User.objects.create_superuser(_USER, "probe@zinnia.local", _PASS)

# ---- 内容种子:作者 + 分类 + 两篇已发布 Entry + 标签 ----
from django.contrib.sites.models import Site  # noqa: E402

from zinnia.managers import PUBLISHED  # noqa: E402
from zinnia.models import Author, Category, Entry  # noqa: E402

_site = Site.objects.get(pk=settings.SITE_ID)
_author = Author.objects.get(username=_USER)
_cat, _ = Category.objects.get_or_create(title="sfcat", slug="sfcat")
_e1, _ = Entry.objects.get_or_create(
    slug="first-entry",
    defaults=dict(title="First entry", content="hello sf world",
                  status=PUBLISHED))
_e1.authors.add(_author)
_e1.categories.add(_cat)
_e1.sites.add(_site)
_e1.tags = '"sftag" "sfcase"'
_e1.save()
_e2, _ = Entry.objects.get_or_create(
    slug="second-entry",
    defaults=dict(title="Second entry", content="second body",
                  status=PUBLISHED))
_e2.authors.add(_author)
_e2.sites.add(_site)
_e2.save()

from django.test import Client  # noqa: E402

CLIENT = Client()
CLIENT.login(username=_USER, password=_PASS)

_d = _e1.publication_date
_Y, _M, _Dy = _d.year, f"{_d.month:02d}", f"{_d.day:02d}"
# 短链 token:base36(pk)(zinnia/url_shortener,默认 DefaultShortener)
from zinnia.url_shortener import get_url_shortener  # noqa: E402

_SHORT = urlsplit(get_url_shortener()(_e1)).path

ROUTES: list[tuple[str, str]] = [
    # 归档体系(zinnia/urls/archives.py)
    ("GET", "/blog/"),
    ("GET", "/blog/page/1/"),
    ("GET", f"/blog/{_Y}/"),
    ("GET", f"/blog/{_Y}/{_M}/"),
    ("GET", f"/blog/{_Y}/{_M}/{_Dy}/"),
    ("GET", f"/blog/{_Y}/week/42/"),
    ("GET", "/blog/today/"),
    # 条目详情 + 短链(zinnia/urls/entries.py + shortlink.py)
    ("GET", f"/blog/{_Y}/{_M}/{_Dy}/first-entry/"),
    ("GET", _SHORT),
    # 作者/分类/标签维(zinnia/urls/{authors,categories,tags}.py)
    ("GET", "/blog/authors/"),
    ("GET", "/blog/authors/probe/"),
    ("GET", "/blog/categories/"),
    ("GET", "/blog/categories/sfcat/"),
    ("GET", "/blog/tags/"),
    ("GET", "/blog/tags/sftag/"),
    # 搜索/随机/HTML sitemap(zinnia/urls/{search,random,sitemap}.py)
    ("GET", "/blog/search/?pattern=first"),
    ("GET", "/blog/random/"),
    ("GET", "/blog/sitemap/"),
    # 全部 feed 家族(zinnia/urls/feeds.py)
    ("GET", "/blog/feeds/"),
    ("GET", "/blog/feeds/discussions/"),
    ("GET", "/blog/feeds/tags/sftag/"),
    ("GET", "/blog/feeds/authors/probe/"),
    ("GET", "/blog/feeds/categories/sfcat/"),
    ("GET", f"/blog/feeds/comments/{_Y}/{_M}/{_Dy}/first-entry/"),
    # 能力声明(zinnia/urls/capabilities.py)
    ("GET", "/blog/humans.txt"),
    ("GET", "/blog/rsd.xml"),
    ("GET", "/blog/opensearch.xml"),
    ("GET", "/blog/wlwmanifest.xml"),
    # 引交互动(trackback/comments/quick-entry;GET 为 3xx/4xx 语义)
    ("GET", "/blog/trackback/1/"),
    ("GET", "/blog/quick-entry/"),
    ("GET", "/blog/comments/success/"),
    # 宿主级 sitemap + admin + django_comments
    ("GET", "/sitemap.xml"),
    ("GET", "/sitemap-blog.xml"),
    ("GET", "/admin/"),
    ("GET", "/admin/zinnia/entry/"),
    ("GET", "/admin/login/"),
    ("GET", "/comments/post/"),
    ("GET", "/blog/nonexistent/"),
]


def smoke() -> list[tuple[str, str | int]]:
    out = []
    for method, path in ROUTES:
        try:
            r = CLIENT.get(path) if method == "GET" else CLIENT.post(path)
            out.append((f"{method} {path}", r.status_code))
        except Exception as exc:                 # noqa: BLE001
            out.append((f"{method} {path}", f"EXC:{type(exc).__name__}:{exc}"[:160]))
    return out


if __name__ == "__main__":
    results = smoke()
    ok = sum(1 for _, s in results if isinstance(s, int) and s < 500)
    print(f"smoke: {ok}/{len(results)} non-5xx site={_SITE}")
    for route, status in results:
        print(f"  {status!s:>4}  {route}")
