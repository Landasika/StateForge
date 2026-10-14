# microblog 状态语义笔记(门 4)

应用:Miguel Grinberg Flask Mega-Tutorial 完整应用(Flask 3.0 +
SQLAlchemy 2.1 + flask-login/flask-mail/flask-babel/rq)。sqlite 每进程
路径;源码 external/microblog,venv external/microblog/.venv。批 1。

## 跨请求守卫实例(≥2)

### G1 SESSION 域:flask-login 会话门(@login_required 全家)
- 位置:`app/main/routes.py` 各视图(index/explore/edit_profile/messages/
  notifications/search/export_posts/follow/unfollow/send_message/
  translate)+ `app/auth/views.py::login`(核验 `User.check_password`)
- 谓词:`session["_user_id"] == <id>`(login_manager 载入 current_user)
- 生产者:`POST /auth/login`(WTF 表单,CSRF 默认开 —— harness 以
  TESTING + WTF_CSRF_ENABLED=False 直 POST)
- 单请求 fuzz 不可达:未登录 → 302 /auth/login,深块(博客列表/表单处理)
  零覆盖;test_client 保持会话 cookie 才能穿过

### G2 DB+SESSION 域:关注关系(follow/unfollow 原子对)
- 位置:`app/main/routes.py::follow/unfollow` — `current_user.follow(user)`
  / `unfollow(user)`(`user.followers.append(...)`,关联表落库)
- 谓词:`EXISTS followers(follower_id=me, followed_id=<username>)`
- 生产者:`POST /follow/<username>`;消费:`/user/<u>` 的
  follow/unfollow 按钮、`/api/users/<id>/followers`
- 单请求 fuzz 不可达:重复 follow 冲突 / 未登录 302 / 用户不存在 404;
  状态翻转改变后续所有 /index 列表(post_stream 并集)

### G3 DB 域:私信(unread 计数跨请求累积)
- 位置:`app/main/routes.py::send_message` — `Message(sender=me,
  recipient=peer, body=...)` + last_message_time 更新;`messages()` 页按
  `User.last_message_read_time` 过滤 unread
- 谓词:`EXISTS User[last_message_read_time < max(Message.timestamp)]`
- 生产者:`POST /send_message/<recipient>`(同时给收件人发 email 通知,
  TESTING 下 flask-mail 抑制)
- 状态迁移:收件人 GET /messages → last_message_read_time 推进 → unread
  归零;导航栏 unread 徽标随之变化

### G4 TOKEN 域:API 蓝图 Bearer/Basic 双门
- 位置:`app/api/auth.py`(basic_auth/`verify_password`)与各
  `@token_auth.login_required` 视图(`/api/users` 家族)
- 谓词:`User.token == <Bearer>`(或 Basic 凭据)
- 生产者:`POST /api/tokens`(Basic)→ `User.get_token()` 签发
- 单请求不可伪造:token 为 64 hex 随机;冒烟里先引导 token 再带
  Bearer 头,401 分支单独留一条做守卫展示

### 附:rq 任务门(export_posts)
- `current_app.task_queue.enqueue('app.tasks.export_posts', ...)` 真连
  redis(localhost:6379)→ 无 redis 进程时 ConnectionError 5xx;harness 以
  `_FakeTaskQueue` stub 顶替(get_id 语义保持,Task 行照常落库)

## 状态域覆盖
- SESSION:G1(登录会话)
- DATABASE:G2(followers 关联)/ G3(Message/last_message_read_time)/
  G4(User.token)
- FILE:无(sqlite DB 域);URL:无(query 皆为瞬时参数)

## 接入门记录(2026-10-14)
- 门 1:28/28 non-5xx(冒烟表 harness.smoke();/search?q= 为 302 重定向,
  /user/ghost 404,/api/users 无 token 401)
- 门 2:SF_DB 每进程默认 `mkdtemp(含 pid)/microblog.db`,双进程并行
  28/28+28/28 不串库 ✓;显式共享同一 SF_DB 且并发写会触 sqlite 锁竞争
  (单进程 28/28 正常)
- 门 3:build_icfg 98 fn/600 blocks/613 edges;analyze(flask)
  27 ep/26 cand,<1s,venv 零泄漏
- venv:external/microblog/.venv(anaconda py3.13 基座);补装
  `--no-cache-dir -U "SQLAlchemy>=2.0.36"`(2.0.23 的 TypingOnly 断言在
  py3.13 崩:`__firstlineno__`/`__static_attributes__`)+ 
  `typing_extensions>=4.12`(旧版 TypeVar.__default__ 只读)
- 配置:DATABASE_URL/SECRET_KEY env 先于 import config;TESTING=True
  关 SMTP handler 与 logs/ 目录;MAIL_SERVER=None;ELASTICSEARCH_URL=
  None(Elasticsearch 不实例化);REDIS_URL 仅惰性 from_url
