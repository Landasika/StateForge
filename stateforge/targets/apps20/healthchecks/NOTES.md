# healthchecks 状态语义笔记(门 4)

应用:uptime 监控服务(Django 6.1,生产级,sqlite)。批 1。

## 跨请求守卫实例(≥2)

### G1 会话域:@login_required(front 全家)
- 位置:`hc/front/views.py` 各视图 `@login_required`(如 `checks`, `details`)
- 谓词:`Session.authed == True`
- 生产者:`POST /accounts/login/`(hc.accounts.views.login)
- 单请求 fuzz 不可达:未登录 → 302 login,深块(渲染 checks 列表/表单处理)零覆盖

### G2 DB 域:Check 按 code 定位(ping 族,capability token)
- 位置:`hc/api/views.py::ping` — `Check.objects.get(code=code)`(get_object_or_404 族)
- 谓词:`EXISTS Check[code]`
- 生产者:`POST /api/v3/checks/`(创建 check 返回 code)/ `POST <project>/checks/add/`
- 状态迁移:ping/fail/start/log 写 `Check.status`(new→up/down)与 Ping 记录

### G3 DB 域:API key 门(哈希双 key)
- 位置:`hc/api/views.py` — `Project.objects.for_api_key(X-Api-Key, ...)`
- 谓词:`Project.api_key == <hcw_...>`(存储为哈希,明文仅生成时可见)
- 生产者:项目设置页生成 key(`Project.set_api_key()`)
- 单请求不可伪造:22+ 字符高熵随机

### G4 会话+DB 复合:pause/resume 状态原子
- `POST /api/v3/checks/<code>/pause` → `Check.status=paused`;resume 恢复
- 与 G2/G3 叠加:先构造 check(key 门),再迁移状态

## 状态域覆盖
- SESSION:G1(登录会话)
- DATABASE:G2(check 存在性)/G3(key)/G4(生命周期 new→up/down/paused)
- FILE:无(纯 DB 应用)

## 接入门记录(2026-10-14)
- 门 1:25/25 non-5xx(冒烟表 harness.smoke())
- 门 2:SF_DB 每进程 sqlite,双进程不串库 ✓
- 门 3:build_icfg 760 fn/6637 blocks/7540 edges;analyze(django) 193 ep/99 cand,venv 零泄漏
- venv:external/healthchecks/.venv(anaconda py3.13,oncalendar 需 --no-cache-dir)
