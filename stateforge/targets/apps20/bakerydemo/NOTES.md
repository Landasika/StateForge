# bakerydemo 状态语义笔记(门 4)

应用:Wagtail 8 官方 demo CMS(Django 6,生产框架)。批 1。

## 跨请求守卫实例(≥2)

### G1 会话域:wagtail admin 登录门
- 位置:`/admin/` 全家(wagtailadmin 内建 permission 装饰器)
- 谓词:`Session.authed ∧ staff`
- 生产者:`POST /admin/login/`(superuser 引导见 harness)
- 未登录 → 302;/admin/pages/ 等深块零覆盖

### G2 DB 域:页面树路由(serve 视图按 slug 定位)
- 位置:wagtail catch-all serve → `Page.route(path)` 沿树查找
- 谓词:`EXISTS Page[tree_path=/probe/deep] ∧ live`
- 生产者:admin 页面创建(harness 经 ORM `add_child` 引导同构)
- 页面树 = 深度多级的持久状态:删父页 → 子路由整体消失

### G3 DB 域:draft/live 发布状态
- 位置:wagtail serve 仅服务 `live=True` 页;`latest_revision` 门预览
- 谓词:`Page.live == True`(draft 页 404)
- 生产者:admin publish 动作(`POST /admin/pages/<id>/actions/publish/`)

### G4 API 域:wagtail API v2 公开读 + type 过滤
- `/api/v2/pages/`、`/api/v2/pages/<id>/`(深块:序列化字段循环)

## 状态域覆盖
- SESSION:G1
- DATABASE:G2(页面树)/G3(发布状态)
- FILE:media 文档/Images(辅助)

## 诚实边界
- 代码 URL 面极小(analyze endpoints=1):Wagtail 路由为 **DB 页面树驱动**,
  静态分析看到的是 catch-all serve;状态语义主战场在页面树/发布流,
  恰是"结构图看不见、状态分析能看见"的典型样本。
- ICFG 76 fn/394 blocks:demo 业务代码薄(逻辑在 wagtail 包内,不入靶 IR)。

## 接入门记录(2026-10-14)
- 门 1:20/20 non-5xx(含 /probe/、/probe/deep/ 页面树路由 200)
- 门 2:SF_DB(DATABASE_NAME)每进程 sqlite ✓
- 门 3:build_icfg 76 fn/394 blocks/368 edges,venv 零泄漏;analyze(django) AppModel 产(端点=1,见诚实边界)
- venv:external/bakerydemo/.venv(wagtail 8.0/django 6.0.9);harness 需
  os.chdir(REPO)(TEMPLATES DIRS 为相对路径)
