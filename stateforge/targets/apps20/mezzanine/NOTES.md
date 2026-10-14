# Mezzanine 状态语义笔记(门 4)

应用:Mezzanine(库式 CMS,mezzanine master 9999.dev0 -e,Django 5.2.18
+ django-contrib-comments 2.2.0,py3.13.5)。批 3,库+宿主脚手架模式。

宿主:官方 `mezzanine/project_template/project_name` 每进程拷贝到临时目录,
`sf_settings.py`(`from .settings import *` + SECRET_KEY/ALLOWED_HOSTS=*/
sqlite/mezzanine.accounts)+ 重写 `urls.py`(admin 真实挂载 + 首页走
`pages.views.page {"slug": "/"}`,官方模板注释里的第二种首页接法,
因模板 `index.html` 不存在)。与 `tests/conftest.py` 的脚手架手法一致。

## 跨请求守卫实例(≥2)

### G1 会话域:staff 前台编辑链(/edit/ + /set_site/)
- `POST /edit/`(inline edit)先 `apps.get_model(POST["app"],...)` 取对象,
  再 `is_editable(obj, request)` + `has_site_permission(user)`
  (mezzanine.core.views / mezzanine.utils.sites)——双谓词守卫;
- `GET /set_site/?site_id=N`:staff_member_required 门 + SitePermission 校验
  (非 superuser 必须有该 site 的权限),**写入 `request.session["site_id"]`**,
  后续请求经 `CurrentSiteManager` 读该会话值过滤站点内容——
  典型"写会话 → 改变后续请求可见数据集"的跨请求状态机;
- 冒烟实测顺序敏感佐证:把 `/accounts/logout/` 放在 `/admin/` 之前会令
  后者 302(会话失效),故 ROUTES 将 logout 排在末尾。

### G2 DB 域:发布状态机(BlogPost.status / Page.in_sitemap)
- `BlogPost.publish_date/status`(Displayable)驱动 `/blog/` 列表、
  `/blog/<slug>/` 详情、`/sitemap.xml`(DisplayableSitemap 只出已发布
  且 in_sitemap 的对象):改一行 status → 三族路由输出同步变化;
- 页树(Page.slug 层级)是 `/`(slug="/")、`/about/`、`/gallery/`
  (Gallery 为 Page 子类,走 pages catch-all)的解析来源,
  slug 变更即路由可见性变更。

### G3 复合:评论审核 + 账号门
- `/comment/`(mezzanine.generic)+ django_comments:评论提交后按
  COMMENT_MODERATION 设置进入待审/直接可见状态,详情页/最近评论区渲染
  随审核状态变化;
- mezzanine.accounts(`/accounts/login|signup|logout/`、`/users/<name>/`)
  会话生命周期 + ACCOUNTS_VERIFICATION_REQUIRED 邮箱验证链。

## 状态域覆盖
- SESSION:G1(set_site 写 site_id;登录/登出);DATABASE:G2(Page 页树/
  发布状态/评论审核);FILE:无(静态资源由 staticfiles 托管)。

## 接入门记录(2026-10-14)
- 门 1(冒烟):29/29 non-5xx(EXC/5xx=0;含 POST /edit/ 与
  /set_site/?site_id=1 的带参请求;`__main__` 顺序:logout 最后)。
- 门 2(隔离):SF_DB 覆写 `settings.DATABASES["default"]["NAME"]`,
  每进程独立 sqlite;migrate **全绿,无 --fake 跳过**(Django 5.2 下
  mezzanine master 46 个迁移直接通过)。
- 门 3(静态):`build_icfg(external/mezzanine)` + analyze(django):
  `icfg fn=551 blocks=4940 edges=5580 | ep=25 cand=15`(主环境 python,
  <1min)。
- venv:external/mezzanine/.venv(pip 装 Django 5.2.18 即可,无需其他手术)。
  踩坑两条:
  1. `mezzanine-project` console script 依赖 `distutils.dir_util`
     (py3.12 移除,venv 无 setuptools)→ 弃用,harness 内 copytree
     project_template(与官方 conftest 同法);
  2. settings.py 的 `import imp` 分支仅在存在 local_settings.py 时触发,
     宿主不写 local_settings.py 即绕开;SECRET_KEY/DB 在 sf_settings 补。
