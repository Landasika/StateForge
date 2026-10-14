# 真实应用靶标清单(20 靶,定稿)

状态:**20/20 接入门全过**(2026-10-14;结果表见 results/targets20/gates.md,
逐靶状态语义笔记见 stateforge/targets/apps20/<slug>/NOTES.md)。
本文是 P4.3 状态感知路线与后续评测的靶标唯一口径;微靶
(t_state_chain/gate/shop、microplus)保留为机制开发/回归锚,不占 20 席。
1 顶替记录:FlaskBB 出局(py3.13 无解)→ MoinMoin 入批 3。

## 选型硬约束

1. **Python Web 框架**:Flask/Django 直接接;WSGI 原生(Radicale/Trac)
   经一次性薄适配层;
2. **进程内可执行**:test_client 驱动,sqlite/文件存储,每进程 DB 隔离
   (排除强制 postgres/redis/celery:Ghostwriter/umap/Baserow/NetBox/Saleor);
3. **真实出身**:生产级 OSS 或知名安全基准;**自建靶(vulnshop)不占席**
   ——降级为机制校准靶(回归锚 + Phase-2 目标推导对齐基准);
4. **跨请求状态语义**:每靶登记 ≥2 个跨请求守卫实例(状态阻挡分支主张)。

## 定稿清单

### 批 1:已入库(8,零克隆成本)

| # | 应用 | 框架 | 状态域 | 库内现状 |
|---|------|------|--------|----------|
| 1 | healthchecks | Django | 会话+DB(监控项生命周期) | external/ 已克隆,sqlite 默认,缺 harness |
| 2 | bakerydemo | Django(Wagtail) | 会话+DB(页面/表单工作流) | external/ 已克隆,缺 harness |
| 3 | realworld-django-ninja | Django Ninja | Token+DB(文章/关注) | harness+fuzz_target 已接线 |
| 4 | BenchmarkPython | Flask | 混合(OWASP 基准多路由) | benchmark_baseline 已集成 |
| 5 | FlaskBB | Flask | 会话+DB(帖子/权限) | **出局**(2026-10-14 接入门:flask-allows git-only 缺位 + flask._compat 已随 Flask 2.1 移除,py3.13 无解)→ MoinMoin 顶替入批 3 |
| 6 | VAmPI | Flask+SQLAlchemy | Token+DB(用户 CRUD) | adapter 已有 |
| 7 | PyGoat | Django | 会话+DB(挑战状态) | targets/pygoat 源码在库 |
| 8 | DSVW | 单文件 WSGI | 会话+sqlite | targets/dsvw 在库(OWASP 名录 VWA) |

### 批 2:待克隆(10,Flask/Django)

| # | 应用 | 框架 | 状态域 | 备注 |
|---|------|------|--------|------|
| 9 | CTFd | Flask | 会话+DB(竞赛/提交) | 已在线核实;sqlite dev |
| 10 | linkding | Django | 会话+DB(书签/标签) | 已在线核实;sqlite 默认 |
| 11 | Fava | Flask | **文件域**(beancount 账本) | 文件状态主张主靶 |
| 12 | OctoPrint | Flask | 会话+文件+sqlite | 大体量真实(3D 打印服务器) |
| 13 | Spirit | Django | 会话+DB(论坛) | sqlite |
| 14 | django-blog-zinnia | Django | 会话+DB(评论流) | 维护模式(如实披露) |
| 15 | Mezzanine | Django | 会话+DB(CMS 页面树) | 维护模式(如实披露) |
| 16 | wger | Django | Token+DB(训练日志) | sqlite dev |
| 17 | django-oscar sandbox | Django | 会话+DB(**订单/支付流**) | 与 create→pay→ship 主张同构 |
| 18 | microblog(Mega-Tutorial) | Flask | 会话+DB(关注/私信链) | 功能完整教程级 |

### 批 3:WSGI 薄适配层后(3,含顶替)

| # | 应用 | 框架 | 状态域 | 备注 |
|---|------|------|--------|------|
| 19 | Radicale | WSGI | **文件域**(CalDAV 集合) | 需 WSGI→test-client 适配层 |
| 20 | Trac | WSGI | **sqlite 原生**(ticket 工作流) | 同上;老牌真实应用 |
| 21 | MoinMoin | WSGI | **文件域**(wiki 页面/用户) | FlaskBB 出局后顶替(备补池首项) |

**备补池**(任一候选接入门失败时顶替,顺序即优先级):
MoinMoin → Lektor admin → Weblate → Kallithea → docat → ArchiveBox。

## 接入门(每应用过 4 关才算入列)

1. test_client 冒烟:路由表 ≥20 路由 2xx/3xx/4xx 达成;
2. DB 隔离:每进程独立 sqlite(SF_DB 约定),跨轮零污染;
3. 静态:build_icfg(套件目录) 非零块、analyze 产 AppModel;
4. 状态语义笔记:≥2 个跨请求守卫实例登记(域/谓词/生产者)。

## 基础设施

- harness 四件套模板 = targets/realworld/(harness.py 96 行为样板):
  克隆 → harness(路由表+认证引导+每进程 sqlite) → fuzz_target → analyze;
- WSGI 适配层(批 3 前置):werkzeug test_client 直驱 WSGI app,
  路由表手工登记(两应用分摊成本)。

## 决策记录

- 2026-10-14:20 靶定稿;WSGI 两靶保留(文件域/sqlite 状态语义难得);
  vulnshop 降为校准靶不占席;DSVW 按 OWASP 名录 VWA 计入真实应用。
- 风险披露:批 2 中 10 靶仅 CTFd/linkding 经在线核实,其余以接入门
  兜底验证;zinnia/mezzanine 维护模式入列(框架/领域多样性),论文披露。

## 接入门结果摘要(2026-10-14,20/20 全过)

门 1:533 条冒烟路由全 non-5xx;门 2:每进程 DB 隔离 20/20;
门 3:ICFG 合计 ~16 万块(最大 OctoPrint 34598),AppModel 端点 0 的
4 例为路由形态边界(realworld=ninja 装饰器/dsvw+Radicale+Trac=非框架
路由/oscar=sandbox include;zinnia=url() 老写法),如实记录;
门 4:每靶 NOTES ≥2 跨请求守卫实例。
逐靶数字与完整踩坑总账见 results/targets20/gates.md(不入库,工作树内)。

## 环境备忘(py3.13 接入踩坑,复现要点)

- venv=anaconda py3.13;pip 一律 `--no-cache-dir`;
- 二进制适配:psycopg2-binary≥2.9.10 / SQLAlchemy≥2.0.36 /
  cffi·argon2-cffi·greenlet 升级 / setuptools<81(pkg_resources,Trac);
- 老栈:zinnia 需 Django 2.2.28+legacy-cgi+setuptools;
- 相对路径配置需 chdir/双 sys.path(vampi/bakerydemo/BenchmarkPython/
  oscar);spirit CLI 需 venv bin 上 PATH;
- 框架怪癖:CTFd(set_config 缓存/SECRET_KEY_FILE 改道)、moin(官方
  CLI 绕 WikiConfigProtocol)、oscar(catalogue.0017 --fake)、
  linkding(huey data/ 目录)、wger(loaddata+GymConfig pk=1+i18n 前缀);
- 基础设施:adapters/wsgi.py(任意 WSGI→GenericWebAdapter)。
