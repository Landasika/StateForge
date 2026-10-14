# BenchmarkPython 状态语义笔记(门 4)

应用:OWASP Benchmark for Python v0.1(Flask,1231 用例模块/2464 路由,
安全基准)。批 1;cwd 须为仓库根(testcode/ 相对扫描)。

## 跨请求守卫实例(≥2)

### G1 会话域:用例间 session 向量
- 部分 XSS/securecookie 用例经 flask session 存取攻击向量
  (app.secret_key 固定,会话可跨请求构造)

### G2 DB 域:sqlite 用例数据
- trust-sec/SQLI 族用例读写用例内 sqlite/数据表(secret 值比对)

### G3 弱随机/状态(cookie)
- weakrand 族:cookie 值预测(跨请求可预测性 = 状态依赖样本)

## 状态域覆盖
- SESSION:G1/G3;DATABASE:G2(用例级);FILE:模板 send_from_directory

## 接入门记录(2026-10-14)
- 门 1:26/26 non-5xx(等距采样 24 用例路由 + 根路由;2464 rules)
- 门 2:进程内无共享库文件(用例数据在内存/临时);采样口径下无跨轮污染
- 门 3:见 gates 表(后台回填)
- venv:external/BenchmarkPython/.venv(102 依赖冻结清单)
