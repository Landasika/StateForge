# DSVW 状态语义笔记(门 4)

应用:Damn Small Vulnerable Web(OWASP 名录 VWA,单文件 stdlib)。批 1。
形态:http.server 线程服务 + :memory: sqlite → 进程内回环驱动
(批 3 WSGI/HTTP 回环适配层原型)。

## 跨请求守卫实例(≥2)

### G1 DB 域:comments 写/读对
- 写:`GET /?comment=<text>` → `INSERT INTO comments`(dsvw.py:42)
- 读:`GET /?comment=`(空值)→ `SELECT id, comment, time`(dsvw.py:45)
- 跨请求语义:先写后读才见内容;:memory: 库随进程生命周期

### G2 会话域:/login 核验 + SESSIONID cookie
- 位置:dsvw.py:60-63 — `SELECT * FROM users WHERE username=... AND password=...`
- 命中 → `Set-Cookie: SESSIONID=<20 字符随机>`;未中 → 清除 cookie(expires 过去)
- users 表由 init() 种子(admin 等)——会话构造的生产者依赖

### G3 DB 域:users 按 id 查询
- `GET /?id=1` → `SELECT ... WHERE id=<n>`(dsvw.py:22);表状态种子

## 状态域覆盖
- SESSION:G2(SESSIONID 置/清)
- DATABASE:G1(comments)/G3(users)
- FILE:无(但存在 path= 任意读 sink——fuzz 漏洞面,冒烟不触)

## 诚实边界
- analyze(AppModel)端点=0:dsvw 无框架路由装饰器,静态词汇看不见其
  路由面;其执行反馈依赖回环 harness(结构图 vs 状态分析的对照样本)。
- ICFG 仅 3 fn/55 blocks(单文件):块数小但覆盖主张按比例。

## 接入门记录(2026-10-14)
- 门 1:22/22 non-5xx(良性参数面,冒烟避开 path/include/domain/object sinks)
- 门 2::memory: sqlite 进程天然隔离 ✓
- 门 3:build_icfg 3 fn/55 blocks/73 edges;AppModel 产(空,见诚实边界)
- 运行:主环境即可(纯 stdlib,无需 venv)
