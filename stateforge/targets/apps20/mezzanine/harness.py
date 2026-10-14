"""Mezzanine 靶 harness(库式 CMS,Django 5.2 + sqlite,批 3)。

形态:Mezzanine 是可安装 Django 应用库,宿主项目由官方
`mezzanine/project_template` 脚手架生成 → 每进程拷贝 project_template 到
临时目录并覆写 urls/sf_settings → migrate → superuser + 内容种子
(Page 页树 / BlogPost / Gallery)→ Client 会话。

注:`mezzanine-project` console script 依赖 distutils(py3.12 已移除,
venv 无 setuptools),故 harness 内直接 copytree project_template,
与官方 tests/conftest.py 的做法一致(库+宿主脚手架模式)。

状态语义(NOTES.md 详):
- 会话域:/edit/ 前台编辑门(staff)、mezzanine.accounts 登录链;
- DB 域:Page 页树(sitemap.xml/菜单跨请求派生)、BlogPost 发布状态
  (status 驱动 /blog/ 列表与 sitemap 输出)、评论审核状态。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[4] / "external" / "mezzanine"

# ---- 每进程宿主脚手架:拷贝官方 project_template/project_name ----
_SITE = Path(tempfile.mkdtemp(prefix=f"mezz_site_{os.getpid()}_"))
shutil.copytree(REPO / "mezzanine" / "project_template" / "project_name",
                _SITE / "project_name")

_HOST_URLS = '''\
from django.contrib import admin
from django.urls import include, path

from mezzanine.pages import views as pages_views

admin.autodiscover()

urlpatterns = [
    path("admin/", include(admin.site.urls)),
    # 首页用页树里的 "/" 页(官方模板注释里的第二种接法),模板 index.html 不存在
    path("", pages_views.page, {"slug": "/"}, name="home"),
    path("", include("mezzanine.urls")),
]
'''

_HOST_SETTINGS = '''\
from .settings import *  # noqa: F401,F403

SECRET_KEY = "sf-mezzanine-harness-key"
ALLOWED_HOSTS = ["*"]
DEBUG = False

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": "",  # harness 导入后按 SF_DB 覆写
    }
}

# 官方模板默认注释掉的 accounts 应用:提供 /accounts/* 会话门路由
INSTALLED_APPS = list(INSTALLED_APPS) + ["mezzanine.accounts"]
ACCOUNTS_PROFILE_VIEWS_ENABLED = True
'''

(_SITE / "project_name" / "urls.py").write_text(_HOST_URLS)
(_SITE / "project_name" / "sf_settings.py").write_text(_HOST_SETTINGS)

os.environ["DJANGO_SETTINGS_MODULE"] = "project_name.sf_settings"
sys.path.insert(0, str(_SITE))

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

_DB = Path(os.environ.get("SF_DB", _SITE / f".sf_db_{os.getpid()}.sqlite3"))
settings.DATABASES["default"]["NAME"] = str(_DB)

from django.core.management import call_command  # noqa: E402

# Django 5.2 下 mezzanine master 全部迁移直接通过,无需 --fake 跳过
call_command("migrate", verbosity=0, interactive=False, run_syncdb=True)

_USER, _PASS = "probe", "Passw0rd!"
from django.contrib.auth import get_user_model  # noqa: E402

User = get_user_model()
if not User.objects.filter(username=_USER).exists():
    User.objects.create_superuser(_USER, "probe@mezzanine.local", _PASS)

# ---- 内容种子:页树 + 博客 + 相册(给 detail/archive 路由真实对象) ----
from django.contrib.contenttypes.models import ContentType  # noqa: E402
from django.utils import timezone  # noqa: E402

from mezzanine.blog.models import BlogCategory, BlogPost  # noqa: E402
from mezzanine.galleries.models import Gallery  # noqa: E402
from mezzanine.generic.models import AssignedKeyword, Keyword  # noqa: E402
from mezzanine.pages.models import RichTextPage  # noqa: E402

_user = User.objects.get(username=_USER)
_now = timezone.now()
RichTextPage.objects.get_or_create(
    slug="/", defaults=dict(title="Home", content="sf harness home"))
RichTextPage.objects.get_or_create(
    slug="about", defaults=dict(title="About", content="sf about page"))
_cat, _ = BlogCategory.objects.get_or_create(title="sfcat")
_post1, _ = BlogPost.objects.get_or_create(
    slug="first-post",
    defaults=dict(title="First post", content="hello world", user=_user,
                  publish_date=_now, status=2))          # status=2 已发布
_post1.categories.add(_cat)
_kw, _ = Keyword.objects.get_or_create(title="sftag")
AssignedKeyword.objects.get_or_create(
    keyword=_kw, object_pk=str(_post1.id),
    content_type=ContentType.objects.get_for_model(_post1))
BlogPost.objects.get_or_create(
    slug="second-post",
    defaults=dict(title="Second post", content="second body", user=_user,
                  publish_date=_now, status=2))
Gallery.objects.get_or_create(slug="gallery", defaults=dict(title="My Gallery"))

from django.test import Client  # noqa: E402

CLIENT = Client()
CLIENT.login(username=_USER, password=_PASS)

_YEAR = _post1.publish_date.year

# /edit/ 是内联编辑表单 POST(读 POST["app"]/["model"]/["id"]/["fields"])
# /set_site/ 读 GET["site_id"];均为 staff 门后视图,带参请求避免 4xx/EXC
ROUTES: list[tuple[str, str] | tuple[str, str, dict]] = [
    # 页树(pages catch-all + 首页)
    ("GET", "/"),
    ("GET", "/about/"),
    ("GET", "/nonexistent-page/"),
    # blog 列表/详情/归档/聚合(来自 mezzanine/blog/urls.py)
    ("GET", "/blog/"),
    ("GET", "/blog/first-post/"),
    ("GET", "/blog/second-post/"),
    ("GET", f"/blog/archive/{_YEAR}/"),
    ("GET", "/blog/category/sfcat/"),
    ("GET", "/blog/tag/sftag/"),
    ("GET", "/blog/author/probe/"),
    ("GET", "/blog/feeds/rss/"),
    # 相册页(Page 子类,走 pages catch-all)
    ("GET", "/gallery/"),
    # core:搜索/sitemap
    ("GET", "/search/?q=post"),
    ("GET", "/sitemap.xml"),
    # generic:评论/评分
    ("GET", "/comment/"),
    ("GET", "/rating/"),
    # core:前台编辑门(staff 会话)
    ("POST", "/edit/", {"app": "blog", "model": "blogpost", "id": _post1.id,
                        "fields": "content", "content": "hello world"}),
    ("GET", "/set_site/?site_id=1"),
    # accounts 会话域(mezzanine.accounts)
    ("GET", "/accounts/"),
    ("GET", "/accounts/login/"),
    ("GET", "/accounts/signup/"),
    ("GET", "/users/"),
    ("GET", "/users/probe/"),
    # 密码重置链(core/urls.py 挂的 auth_views)
    ("GET", "/password_reset/"),
    ("GET", "/password_reset/done/"),
    # admin(模板大量 reverse "admin:*",需真实挂载)
    ("GET", "/admin/"),
    ("GET", "/admin/blog/blogpost/"),
    ("GET", "/admin/pages/page/"),
    # 登出销毁会话,放在最后,避免影响后续路由
    ("GET", "/accounts/logout/"),
]


def smoke() -> list[tuple[str, str | int]]:
    out = []
    for route in ROUTES:
        method, path = route[0], route[1]
        data = route[2] if len(route) > 2 else {}
        try:
            if method == "GET":
                r = CLIENT.get(path)
            else:
                r = CLIENT.post(path, data)
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
