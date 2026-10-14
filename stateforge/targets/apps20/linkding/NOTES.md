# linkding 状态语义笔记(门 4)

应用:自托管书签管理器(Django 6.1,生产级,sqlite)。批 2。

## 跨请求守卫实例(≥2)

### G1 会话域:@login_required(bookmarks 前端全家)
- 位置:`bookmarks/views/bookmarks.py:42`(`index`;同类 `:81 archived`、
  `:221 new`、`:237 edit`、tags/bundles/settings 视图同款装饰)
- 谓词:`Session.authed == True`
- 生产者:`POST /login/`(`bookmarks/views/auth.py:7` LinkdingLoginView)
- 单请求 fuzz 不可达:未登录 → 302 login,列表渲染/表单处理深块零覆盖

### G2 DB 域:Bookmark 存在性 + owner/share 谓词(access 模块)
- 位置:`bookmarks/views/access.py:8-15` `bookmark_read` —
  `Bookmark.objects.get(pk=int(bookmark_id))` → Http404;再判
  `is_owner or (shared and owner.profile.enable_sharing)`;写路径
  `:23 bookmark_write` 更严:`get(pk=..., owner=request.user)`
- 谓词:`EXISTS Bookmark[id] ∧ (owner==user ∨ shared)`
- 生产者:`POST /bookmarks/new`(`bookmarks.py:222`)/ `POST /api/bookmarks/`
- 状态迁移:archive/unarchive(`api/routes.py:88-103`)改 `is_archived`,
  edit 改 title/tags;feeds 的 unread 过滤依赖 `unread` 标记

### G3 capability 域:FeedToken.key(RSS,无会话)
- 位置:`bookmarks/feeds.py:36-37` `get_object` —
  `FeedToken.objects.get(key__exact=feed_key)`(40 字符高熵 hex)
- 谓词:`EXISTS FeedToken[key]`
- 生产者:`/settings/integrations`(`bookmarks/views/settings.py:178`
  `FeedToken.objects.get_or_create(user=request.user)`)
- 单请求不可伪造:40 字符 os.urandom(20) hex

### G4 api-token 域:LinkdingTokenAuthentication(多关键词多令牌)
- 位置:`bookmarks/api/auth.py:17-25` `authenticate` —
  解析 `Authorization: Token|Bearer <key>`,模型 `ApiToken`(`:14`)
- 谓词:`EXISTS ApiToken[key]`(40 字符高熵 hex,同 G3)
- 生产者:`/settings/integrations/create-api-token`(`settings.py`)
- REST API 全家(`/api/...` DEFAULT_PERMISSION_CLASSES=IsAuthenticated)

## 状态域覆盖
- SESSION:G1(登录会话,CLIENT.login 后 cookie 跨请求持久)
- DATABASE:G2(bookmark 存在/owner)、G3(feed token)、G4(api token)、
  Bookmark 生命周期 active↔archived(`is_archived`)、read↔unread
- FILE:`bookmark_deleted` 信号删除 `LD_PREVIEW_FOLDER` 下预览图
  (`bookmarks/models.py:117-127`);favicons/previews/assets 落盘
  `external/linkding/data/`

## 接入门记录(2026-10-14)
- 门 1:39/39 non-5xx(冒烟表 harness.smoke();含 302/404 守卫正常响应)
- 门 2:SF_DB 每进程 sqlite(pid 后缀,支持 env 覆写),双进程不串库 ✓
- 门 3:build_icfg 614 fn/4315 blocks/4684 edges;analyze(django)
  29 ep/33 cand(主环境 python,未超时)
- venv:external/linkding/.venv(Django 6.1.2);坑:huey 在 setup() 时即
  打开 data/tasks.sqlite3 → 须先建 data/ 目录;LD_DISABLE_BACKGROUND_TASKS=True
  关任务入队;/api/bookmarks/check 会真发 HTTP(website_loader),冒烟避开
