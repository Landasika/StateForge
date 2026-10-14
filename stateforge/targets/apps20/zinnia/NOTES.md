# django-blog-zinnia 状态语义笔记(门 4)

应用:django-blog-zinnia 0.20.1.dev0(-e,master,项目止于 Django 2.x 时代),
宿主仿官方 `demo/settings.py`;运行栈 Django 2.2.28 + py3.13.5。批 3。

## 跨请求守卫实例(≥2)

### G1 DB 域:Entry 发布状态机(status/ start_publication)
- `Entry.status`(managers.PUBLISHED=2)+ `is_actual`(publication
  start/end 窗口)共同构成发布谓词(EntryAbstractClass.is_published);
- 所有归档路由(/blog/、/blog/<Y>/<M>/、/blog/today/、week/42/)、
  feed 家族(/blog/feeds/**)、宿主 /sitemap-*.xml、/blog/random/
  (随机已发布条目 302)全部经 `EntryPublishedManager` 过滤——
  改一条 status → 十余族路由输出同步变化,最强跨请求守卫面;
- 未来发布(start_publication 未到)条目在到期前 detail 404。

### G2 DB 域:分类树 + 标签两个派生维
- Category(mptt 树,`categories/<path>/` 支持层级路径)与
  Tag(django-tagging,`tags/<tag>/`、feeds/tags/<tag>/)是 Entry 的
  双向派生索引:建/删分类或标签即时改变对应列表/详情/feed 路由的
  可见集(种子 sfcat/sftag 实测 200)。

### G3 复合:评论审核链 + 引用交互
- django_comments 提交(POST /comments/post/)+ zinnia
  `EntryCommentModerator`(apps.ready 注册)按 AUTO_MODERATE/
  AUTO_CLOSE_EMAILS 决定直发/待审,admin 端(/admin/django_comments/)
  approve 后条目页与 /blog/feeds/comments/** 才更新——
  会话(admin 权限)+ DB(审核状态)复合守卫;
- trackback(/blog/trackback/<pk>/)与 discussion/pingback feed
  计数(comment_count/trackback_count 冗余字段)跨请求更新。

### G4 会话域:staff 快速发文门
- `/blog/quick-entry/`(permission_required zinnia.add_entry):
  匿名 GET 302 跳登录;staff POST 才建 Entry(冒烟中以 superuser
  会话驱动,GET 语义 302)。

## 状态域覆盖
- SESSION:G3(admin 审核)/G4(quick-entry 权限门);
  DATABASE:G1(发布状态机)/G2(分类树+标签);FILE:无。

## 接入门记录(2026-10-14)
- 门 1(冒烟):38/38 non-5xx(EXC/5xx=0)。
- 门 2(隔离):SF_DB 覆写 DATABASES NAME 每进程 sqlite;
  migrate **全绿,无 --fake**(Django 2.2 下 zinnia/tagging/
  django_comments 全部迁移直接通过)。
- 门 3(静态):`build_icfg(external/django-blog-zinnia)` + analyze:
  `icfg fn=371 blocks=2217 edges=2179 | ep=0 cand=0`
  ——ep/cand=0 的原因:zinnia 路由全部用 `django.conf.urls.url()`
  (Django 2.x 写法),分析器 django 插件只认 path/re_path 形态,
  端点提取不到(如实记录;ICFG 本体 fn/blocks/edges 正常)。
- venv 依赖手术链(external/django-blog-zinnia/.venv,pip --no-cache-dir):
  1. 初始 Django 6.1.2:`zinnia/urls/*` 全用 `django.conf.urls.url`
     (4.0 移除)→ ImportError,起不来;
  2. 降到 Django 3.2.25:url()/providing_args/force_text 均过,但
     `zinnia/skeleton.html`(全站基模板)`{% load staticfiles %}`
     3.0 移除 → 全部页面 TemplateSyntaxError(ep:opensearch/wlwmanifest/
     comments-success/sitemap 同炸);
  3. 终态 **Django 2.2.28** + **setuptools 84**(Django 2.2 import
     distutils.version,py3.12+ 靠 setuptools _distutils_hack 顶替)+
     **legacy-cgi**(http 栈 import cgi,PEP 594 移除后官方后继包)+
     **django-mptt 0.12.0 / django-contrib-comments 2.0.0**(Django 2.2 档)
     → 37/38;最后一处 /sitemap.xml NoReverseMatch 由宿主 urls 给
     sitemap 分节 pattern 补 `name="django.contrib.sitemaps.views.sitemap"`
     修复 → 38/38;
  4. **django-xmlrpc 剔除**:0.1.8 `from collections import Callable`
     (py3.10 移除)语法级不兼容且无新版可升;仅 metaweblog/pingback
     可选功能经宿主引用(zinnia 核心/apps.ready 不依赖),宿主
     INSTALLED_APPS 与 urls 不装它即无影响——等价于宿主不启用该功能,
     非代码 shim。
- 其余:Entry.status 为 IntegerField(PUBLISHED=2,勿传字符串);
  authors m2m 收 Author 代理(User 直传报 TypeError);
  短链 token=base36(pk)(/blog/1/ → 301 详情页)。
