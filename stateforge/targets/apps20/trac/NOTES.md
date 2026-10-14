# Trac 状态语义笔记(门 4)

应用:Trac(项目跟踪/issue 系统,老牌生产级,WSGI + sqlite 原生)。
批 3;trac-admin initenv 建环境(每进程临时目录)+ Python API 种子
(ticket×2 + wiki 页)→ dispatch_request(WSGI)→ wsgi_adapter 直驱。

## 跨请求守卫实例(≥2)

### G1 DB 域:ticket 存在性(编号路由)
- 位置:trac.ticket.web_ui — Ticket(env).resource 定位
- 谓词:`EXISTS Ticket[id]`
- 生产者:`POST /newticket`(或 API:Ticket(env).insert())
- harness 实测:种子 ticket #1/#2 → 200;未建编号 → 404

### G2 DB 域:ticket 工作流状态机(new→assigned→closed)
- 位置:trac.ticket.default_workflow
- 谓词:`Ticket.status ∈ {new, assigned, closed, reopened}`
- 迁移动作:accept/resolve/reopen 各自是带前置状态检查的写操作
  (状态构造主张的教科书样本:closed 深块需 status 转移链)

### G3 权限域:匿名只读门
- 位置:trac.perm — TICKET_MODIFY/TRAC_ADMIN 等
- 谓词:`PERMISSION[ticket_modify]`;匿名 → /newticket 403/登录页
- 生产者:trac-admin permission add(权限状态持久于 DB)

### G4 文件域:wiki 页版本史
- WikiPage.save:版本 +1,旧版经 ?version= 可回看;DELETE 需权限

## 状态域覆盖
- DATABASE(sqlite 原生):G1/G2/G3;FILE:G4(attachments 可扩展)

## 接入门记录(2026-10-14)
- 门 1:25/25 non-5xx 全真实 200(含种子 ticket#1/#2 页面)
- 门 2:每进程 initenv 临时目录(内嵌 sqlite),天然隔离
- 门 3:见 gates 汇总表
- venv:external/trac/.venv(setuptools<81 供 pkg_resources + werkzeug)
