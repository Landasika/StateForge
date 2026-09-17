# SF0 —— 底座:环境、依赖、双靶场入场、Atheris 硬门禁

> 状态:进行中(2026-09-17 启动)| 工期:4-5 天 | 依赖:无 | 上游:[00-overview.md](00-overview.md)

## 1. 目标

搭好一切后续里程碑的地基:git 仓库、stateforge 包骨架、ANOTA 解释器内装齐依赖(含 Atheris,硬门禁)、vulnshop 双变体靶场、VAmPI 真实靶场接入。

## 2. 任务分解

### T1 git 仓库与骨架(0.5d)
- [ ] `git init` 于 `/home/landasika/icassp`;`.gitignore`:`anota/`(artifact+构建产物,~GB 级)、`__pycache__/`、`*.pyc`、`stateforge/**/cache/`、`*.db`、`exp/results/`(数据另存或 LFS,先忽略二进制)
- [ ] 首次提交:现有 `exp/ notes/ papers/ plans/ targets/dsvw`(冻结基线可追溯)
- [ ] `stateforge/` 包骨架:空 `__init__.py` ×(core/analyzer/graph/planner/fuzz/executor/snapshot/oracle/llm/eval)+ `cli.py` + `tests/`;`stateforge/py.typed`
- [ ] `pytest.ini`(tests 只依赖系统 python;ANOTA 相关测试打 `@pytest.mark.anota` 标记)

### T2 依赖安装(0.5d;ANOTA 解释器,全部走 tuna 镜像)
- [ ] `sudo apt-get install -y clang`(Atheris 编译需要)
- [ ] **ANOTA 内装,顺序重要——VAmPI 锁定栈优先**(单解释器只能有一个 flask 版本,vulnshop 兼容 2.2.2,故全环境统一 VAmPI 栈):
  ```
  flask==2.2.2 werkzeug==2.3.8 itsdangerous==2.1.2 click==8.1.7 jinja2==3.1.4
  markupsafe==2.1.2 blinker==1.7 connexion==2.14.2 flask-sqlalchemy==3.0.3
  sqlalchemy==2.0.2 PyJWT==2.6.0 jsonschema==4.17.3 PyYAML inflection networkx
  ```
  werkzeug 必须 <3(flask 2.2 的 Header API 兼容);全部纯 Python 或有 cp310 wheel,无需 rustc。
  ⚠️ 实测教训(2026-09-17):先裸装 `flask` 会解析到 3.1.3,须按上表**显式降级**;VAmPI 栈装完后再装 atheris。
- [ ] **atheris 版本约束(实测)**:最新 atheris(2.3.x)用 `dis.Positions`(py3.11+ API),在 ANOTA(3.10.13 fork)上 import 即炸;须装 **2.1.x/2.2.x 线**(py3.10 兼容),逐版本回退 2.1.7 → 2.2.1 → 2.0.12
- [ ] 驱动侧(系统 py3.13,已核实自带 networkx 3.4.2 / httpx 0.28.1 / pydantic 2.11.7)缺什么补什么
- [ ] 产出 `stateforge/requirements-anota.txt` 与 `requirements-driver.txt` 锁定

### T3 Atheris-on-ANOTA 硬门禁(0.5d)⚠️ SF0 出口条件 ✅ **已通过(2026-09-17 预跑)**
- [x] 最小冒烟 harness:`TestOneInput` 内 `data[0]==0x41` 触发 `ANOTA_EXECUTION.BLOCK(False, msg="ANOTA_POLICY:smoke_gate")`
- [x] libFuzzer 记为 crash 并写 artifact(`crash-6dcd4ce2...`;输入 `0x41` 由变异发现 → 覆盖反馈有效)
- **已验证配方(必须遵守)**:`atheris==2.0.12` + **显式 `atheris.instrument_all()`**(Setup 前调用)
  - atheris ≥2.1(2.1.7/2.2.1/2.3.x)在 ANOTA(3.10.13 fork)上 import 即炸:`dis.Positions` 是 py3.11+ API;
  - 不调 `instrument_all()` 则零覆盖("no interesting inputs... instrumented?")。
- **SF2 注意**:libFuzzer 默认首 crash 即退出——多违规收集需 `-fork=1` 或 crash-artifact 去重回灌循环。
- 降级路线(未触发,留档):D1 clang 源码重试;D2 Atheris 跑系统 python + WSGI 桥子进程跑 ANOTA(coverage.py 采集)。

### T4 vulnshop 靶场(2d)—— 详细规格
**文件**:`stateforge/targets/vulnshop/app.py`(~300 行,Flask + 文件 SQLite,stdlib hmac token)+ `app_anota.py`(vuln 固定 + ANOTA 注解)+ `adapter.py`(TargetSpec)+ `golden/`(PoC)+ `tests/`

**实体**:`User(id, username, password, role)`、`Order(id, user_id, status, total, paid_amount)`、`Coupon(code, user_id, used, discount)`

| 端点 | 语义 | 植入漏洞(GUARDS=0) | 漏洞类 |
|---|---|---|---|
| `POST /auth/register` `{username,password}` | 注册 | — | |
| `POST /auth/login` | → `{token}`(hmac) | — | |
| `GET /auth/me` | Bearer 读自己 | — | |
| `POST /orders` `{items, coupon_code?}` | 建 CREATED 订单(此处 redeem 优惠) | (b) redeem 不查/不置 `used` | coupon_reuse |
| `GET /orders/<id>` | 读回 status/total | (c) 缺 owner 检查 | idor |
| `POST /orders/<id>/pay` `{amount}` | 置 PAID | (c) 缺 owner;(d) `amount<total` 仍 PAID | idor+payment_bypass |
| `POST /orders/<id>/ship` | 置 SHIPPED | (a) 无 `status==PAID` 前置 | workflow_bypass |
| `POST /orders/<id>/cancel` | 置 CANCELLED | — | |
| `GET /coupons/<code>` | 读回 used/discount | — | |
| `POST /admin/users/<id>/role` `{role}` | 改角色 | (e) 无 admin 角色检查 | privilege |

- **fixed 变体 = `GUARDS=1` 环境旗标**(ADR-0005):守卫处 `if GUARDS: <check>`;`adapter.init()` 负责开 DB/建 schema/seed 2 张优惠券
- 注解变体 `app_anota.py`:SQL 拼接/文件 sink 处放 `ANOTA_EXECUTION.BLOCK(..., msg="ANOTA_POLICY:<名>")`,风格与 `exp/dsvw_anota.py` 一致(注解必须在 try 覆盖区内——已踩坑规范)
- 约束:同步 server(`threaded=False`);`PRAGMA journal_mode` 断言非 WAL(L2 前提)

### T5 VAmPI 接入(0.5-1d)
- [ ] 下载:`gh-proxy.com/https://github.com/erev0s/VAmPI` tarball → `stateforge/targets/vampi/`,**源码零改动**
- [ ] 启动验证:ANOTA 下 `vulnerable=1 tokentimetolive=600`,`curl 127.0.0.1:5001/v1/` 返回 JSON;`vulnerable=0` 同样跑通
- [ ] `adapter.py`:connexion 的 Flask 内核 `vuln_app.app` 直接包 WSGI(`CountingWSGIServer`,绕过 connexion.run());DB=`database/database.db`;`probes/` 放 8 类漏洞验证探针(SF4 前手写 3 个:BOLA/越权改密/枚举)

## 3. 接口与数据流(本里程碑产出)

```python
# targets/vulnshop/adapter.py
class TargetSpec:
    module: str                 # "stateforge.targets.vulnshop.app"
    init: Callable              # 开 DB + schema + seed(ANOTA 侧执行)
    db_path: Path | None        # L2 模板指向;None=:memory:(纯 COW)
    make_server: Callable       # (app, host, port) -> CountingWSGIServer
    healthy_path: str           # 健康检查路由
    observes: dict              # 端点 → 可读回字段(规划器 verify 用)
```

## 4. 测试

- `stateforge/targets/vulnshop/tests/`:5 类 golden PoC(vuln 变体命中预期状态/响应)+ fixed 反证(GUARDS=1 下 5 PoC 全阻断,脚本断言)+ 1000 请求压测无泄漏
- `app_anota.py` 信道回收:`policies_in()` 风格正则可从响应提取 `ANOTA_POLICY:*`
- pytest 空骨架绿(`pytest -m "not anota"` 在系统 python 可跑)

## 5. 验收清单

- [ ] `git log` 有冻结基线提交;骨架就位
- [ ] ANOTA 内 `import flask, networkx, atheris, connexion, flask_sqlalchemy` 全通过
- [ ] **Atheris-on-ANOTA 冒烟:libFuzzer 记录到注解违规 crash**(硬门禁)
- [ ] vulnshop:vuln 5/5 PoC 命中 + fixed 5/5 阻断 + `app_anota` 信道可回收
- [ ] VAmPI:vulnerable=0/1 双态可服务,`/v1/` 返回 JSON
- [ ] `pytest` 骨架绿

## 6. 风险与回退

| 风险 | 回退 |
|---|---|
| atheris 在 ANOTA fork 上构建失败 | T3 降级路线 D1/D2;D2 触发设计变更评审,不静默绕过 |
| werkzeug/flask 版本冲突 | 已锁定版本表;冲突则降 flask 2.2.x 系内微调 |
| connexion 在 ANOTA 下初始化异常 | 绕过 connexion.run() 直包 WSGI(设计已按此);仍异常则 VAmPI 降级为纯 Flask 装饰器注入层(改动大,需评审) |
