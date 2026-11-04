# 20 靶接入门结果表(gates)——终版

更新:2026-10-14 18:52。**20/20 全部入列**(1 顶替:FlaskBB→MoinMoin)。
门定义见 docs/targets-real-apps.md;realworld 特例:harness/fuzz_target
在 targets/realworld/(既有接线),apps20/realworld/ 仅 NOTES。

## 批 1:库内 7 靶(8 席含出局者)

| # | 靶 | 门1 冒烟 | 门2 DB隔离 | 门3 ICFG | 门3 AppModel | 门4 | venv |
|---|----|---------|-----------|----------|--------------|-----|------|
| 1 | healthchecks | 25/25 | SF_DB | 760fn/6637blk/7540e | 193ep/99cand | ×4 | ✓ |
| 2 | bakerydemo | 20/20 | DATABASE_NAME | 76fn/394blk/368e | 1ep† | ×4 | ✓ |
| 3 | realworld | 20/20 | SF_DB(既有) | 141fn/657blk/604e | 0ep‡ | ×3 | ✓ |
| 4 | BenchmarkPython | 26/26(2464路由) | 用例级内存 | 3710fn/26364blk/27717e | 2463ep/1011cand | ×3 | ✓ |
| 5 | PyGoat | 24/24 | SF_DB 覆写 | 157fn/1209blk/1359e | 229ep/31cand | ×3 | ✓ |
| 6 | VAmPI | 14/14 | createdb+SF_DB | 46fn/424blk/492e | 14ep/16cand | ×3 | ✓ |
| 7 | DSVW | 22/22 | :memory: | 3fn/55blk/73e | 0ep‡ | ×3 | 主环境 |
| — | ~~FlaskBB~~ | **出局**:flask-allows(git-only)+ flask._compat(Flask 2.1 移除),py3.13 无解 → MoinMoin 顶替 | | | | | |

## 批 2:克隆 10 靶

| # | 靶 | 门1 | 门2 | 门3 ICFG | 门3 AppModel | 门4 | venv |
|---|----|-----|-----|----------|--------------|-----|------|
| 8 | CTFd | 28/28 | DATABASE_URL | 841fn/7832blk/9049e | 63ep/116cand | ×4 | ✓ |
| 9 | linkding | 39/39 | mkdtemp+SF_DB | 614fn/4315blk/4684e | 29ep/33cand | ×4 | ✓ |
| 10 | Fava | 30/30 | mkdtemp 账本 | 639fn/3880blk/3852e | 15ep/7cand | ×3 | ✓PyPI |
| 11 | OctoPrint | 32/32 | mkdtemp basedir | 4150fn/34598blk/39123e | 102ep/105cand | ×3 | ✓ |
| 12 | Spirit | 24/24 | mkdtemp 脚手架 | 1055fn/4722blk/4239e | 89ep/50cand | ×4 | ✓ |
| 13 | zinnia | 38/38 | SF_DB | 371fn/2217blk/2179e | 0ep§ | ×2 | ✓(Django2.2 栈) |
| 14 | mezzanine | 29/29 | SF_DB | 551fn/4940blk/5580e | 25ep/15cand | ×2 | ✓ |
| 15 | wger | 40/40 | settings.local_dev | 1226fn/9219blk/9964e | 9ep/9cand | ×4 | ✓ |
| 16 | django-oscar | 24/24 | DATABASE_NAME | 2195fn/12452blk/12285e | 0ep†† | ×4 | ✓+伴生包 |
| 17 | microblog | 28/28 | mkdtemp SF_DB | 98fn/600blk/613e | 27ep/26cand | ×4 | ✓(SA≥2.0.36) |

## 批 3:WSGI 层(含顶替)

| # | 靶 | 门1 | 门2 | 门3 ICFG | 门3 AppModel | 门4 | venv |
|---|----|-----|-----|----------|--------------|-----|------|
| 18 | Radicale | 22/22 | mkdtemp 存储 | 644fn/8796blk/10903e | 0ep‡ | ×4 | ✓+werkzeug |
| 19 | Trac | 25/25 全200 | initenv 临时目录 | 3281fn/27563blk/30958e | 0ep‡ | ×4 | ✓+setuptools<81 |
| 20 | MoinMoin | 23/23 | create-instance 临时目录 | 3344fn/24078blk/25980e | 88ep/82cand | ×4 | ✓ |

† Wagtail 路由为 DB 页面树驱动。
‡ django-ninja 装饰器路由/WSGI/非框架路由形态(执行反馈走 harness,如实边界)。
§ zinnia 用 django.conf.urls.url() 老写法,django_adapter 只认 path/re_path。
†† oscar 经 sandbox urls.py include 形态,静态提取未及。

## 汇总口径

- **门 1**:20/20 通过(共 533 条冒烟路由全 non-5xx);
- **门 2**:20/20 通过(SF_DB 覆写/mkdtemp/:memory:/env-native 四式);
- **门 3**:20/20 通过(ICFG 合计 ~160k 块;AppModel 端点 0 的 4 例均为
  路由形态边界,NOTES 如实记录);
- **门 4**:20/20 NOTES 齐(每靶 ≥2 跨请求守卫实例,多数 ×3~4);
- **仓库回归**:接入期核心改动(icfg/adapter _SKIP 增补、HttpMethod 词表
  扩充 HEAD/OPTIONS、adapters/wsgi.py 新增、apps20 全量)后全量测试
  **451 passed / 1 skipped / 26 deselected,exit 0**。

## 状态域覆盖复核(论文主张面)

- **DB 域 15 靶**(oscar 订单/支付流 = create→pay→ship 旗舰同构);
- **文件域 3 靶**(Fava 账本/Radicale CalDAV/OctoPrint 上传);
- **会话/Token 认证全覆盖**;WSGI 层 3 靶验证了 wsgi_adapter 基础设施。

## 环境备忘(接入门踩坑总账)

- venv 解释器:anaconda py3.13(系统 3.12 缺 ensurepip);pip 全 `--no-cache-dir`;
- py3.13 二进制适配:psycopg2-binary≥2.9.10、SQLAlchemy≥2.0.36、
  cffi/argon2-cffi/greenlet 升级、setuptools<81(pkg_resources);
- 老栈适配:zinnia 需 Django 2.2.28+setuptools(distutils)+legacy-cgi;
  trac 需 setuptools<81;
- 相对路径配置需 chdir 或双 sys.path:connexion spec(vampi)、TEMPLATES
  DIRS(bakerydemo)、testcode/(BenchmarkPython)、ROOT_URLCONF='urls'(oscar)、
  spirit CLI 需 venv bin 上 PATH;
- 框架怪癖:CTFd config 缓存(set_config)+ setup 全站 302 + SECRET_KEY_FILE
  改道;moin 手工 config 过不了 WikiConfigProtocol(官方 CLI 绕开);
  oscar catalogue.0017 迁移确定性失败(--fake);linkding huey 需 data/ 目录;
  wger 需 loaddata 基础表 + GymConfig(pk=1) + i18n 前缀;microblog 需
  SQLAlchemy≥2.0.36 + task queue stub;
- ICFG/适配器 _SKIP 增补 .venv/venv/site-packages;
- 新增基础设施:stateforge/adapters/wsgi.py(任意 WSGI app →
  GenericWebAdapter,werkzeug test Client 直驱,Radicale/Trac 已用)。
