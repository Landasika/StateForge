# realworld-django-ninja 状态语义笔记(门 4)

应用:RealWorld Conduit 规范实现(Django 5.2 + django-ninja,生产规范对标)。
批 1;harness 位于 targets/realworld/(harness.py + generic_target.py,
既有接线,P3/P4 期已用于 realworld 基线)。

## 跨请求守卫实例(≥2)

### G1 Token 域:JWT 认证门(api 全家)
- 位置:`helpers/auth.py`(django-ninja auth header 解析)
- 谓词:`Token.valid(user)`
- 生产者:`POST /api/users/login`(返回 user.token)
- 单请求不可达:无 token → 401/403

### G2 DB 域:文章 slug 路由 + 所有权
- 位置:`apps/articles` — 按 slug 查文章;PUT/DELETE 校验 author==current_user
- 谓词:`EXISTS Article[slug] ∧ Article.author == User`
- 生产者:`POST /api/articles`(201 返回 slug;harness 采集入 _SLUGS 池)

### G3 DB 域:follow 关系(多步社交链)
- `POST /api/profiles/{username}/follow` 前置:双方用户存在;
  feed 深块仅对"已 follow 至少一人"的会话返回非空

## 状态域覆盖
- SESSION(Token):G1;DATABASE:G2(slug/所有权)/G3(follow);FILE:无

## 接入门记录(2026-10-14)
- 门 1:20/20 non-5xx(ROUTES 扩一条查询变体至 20 条口径)
- 门 2:SF_DB 每进程 sqlite(harness 既有设计)
- 门 3:build_icfg/analyze 见 results/(P3 期 realworld 基线已跑通)
- venv:external/realworld-django-ninja/.venv(需 psycopg2-binary≥2.9.10
  适配 py3.13,pyproject 旧 pin 2.9.6 二进制不兼容——已升级)
