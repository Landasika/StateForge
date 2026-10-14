# MoinMoin 状态语义笔记(门 4)

应用:MoinMoin(wiki 引擎,生产级,Flask + 文件域存储)。批 3,
FlaskBB 出局后自备补池顶替。官方 `moin create-instance --full`
生成宿主(每进程临时目录:wikiconfig + wiki 数据 + 索引 + 帮助页)。

## 跨请求守卫实例(≥2)

### G1 会话域:登录门
- 位置:moin.auth / +login 表单
- 谓词:`Session.valid(user)`;harness 实测 /+login POST 200 建会话
- 生产者:+register(或 User API);未登录 /+modify → 403(实测)

### G2 文件域:页面存在性 + 版本史
- 位置:moin.apps.frontend.show_item
- 谓词:`EXISTS Item[name] ∧ version`
- 生产者:+modify(保存→版本+1);/+history/<name> 按版本回看
- harness 实测:/Home 200(内置页)/ NoSuchPageEver 404

### G3 ACL 域:页面级权限
- 位置:moin.security.acl(默认 acl 配置)
- 谓词:`acl[name].read/write`
- 实测:/+admin/trash 403(非 superuser)/+admin/user 200;
  /+modify/Home 403(登录用户但无写权——权利状态真实生效)

### G4 管理域:superuser 门
- /+admin/* 家族:普通用户 403;superuser 200(用户档的 acl 字段)

## 状态域覆盖
- SESSION:G1;FILE:G2(页面树/版本);RIGHTS/DB:G3/G4(用户档)

## 接入门记录(2026-10-14)
- 门 1:23/23 non-5xx(login=200;403/404 为真实门响应)
- 门 2:每进程 mkdtemp create-instance,天然隔离
- 门 3:见 gates 汇总表
- venv:external/moin/.venv(源码 -e 安装)
- 踩坑:手工构造 config 类过不了 WikiConfigProtocol 的
  isinstance(False 但零缺失成员,py3.13 运行时协议检查怪癖)
  → 官方 CLI create-instance 生成的 wikiconfig.py 无此问题
