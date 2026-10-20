# SS-07E/F 报告：五应用冻结预注册与真实启动矩阵

## 状态

**SS-07E 预注册完成；SS-07F 未完成。** 不能把本次结果写成五应用
盲测通过。

```text
Batch: SS-07E/F
Base commit: d363bf8
Pre-registration source revision: d363bf84e5489ffa050655839dca7425a31b38c6
```

## SS-07E：预注册

`stateforge/evaluation/real_apps.py` 在任何启动之前写出
`artifacts/session_ss07ef/pre_registered.json`，包含：

- 五个应用：CTFd、healthchecks、linkding、Spirit、microblog；
- 各自 venv Python、启动命令、health URL、环境变量；
- 每个应用源码树的 SHA-256（忽略 `.venv/.git/__pycache__/data/DB`）；
- 冻结时的 StateForge source revision。

源码树哈希（前 12 位）：

```text
CTFd         c45bd0e0cf92
healthchecks a8eb3ea8b8bb
linkding     4926d735a2d5
Spirit       ffb4571da67c
microblog    d3380292e887
```

## 真实启动矩阵

每个应用使用自身 `.venv`，先执行迁移（Django/CTFd），再启动并访问
health URL；原始 stdout/stderr 逐应用保存。

| 应用 | 状态 | 证据 |
|---|---|---|
| CTFd | SMOKE_PASS | `GET /` → 302，`/setup` → 200 |
| healthchecks | SMOKE_PASS | `GET /` → 200 |
| linkding | SMOKE_PASS | `GET /login` → 301 → 200 |
| Spirit | ENV_BLOCKED | `ModuleNotFoundError: test_project` |
| microblog | SMOKE_PASS | `GET /auth/login` → 200 |

汇总见 `artifacts/session_ss07ef/SS07EF_SUMMARY.json`；逐应用报告
`REPORT_<app>.md`，原始日志 `<app>.log`。

## 为什么没有冻结

`SS07EF_SUMMARY.json` 明确记录：

```json
{
  "arm_runs": 0,
  "blind_fuzzing_claimed": false,
  "frozen": false,
  "freeze_reason": "no real-app Session target adapter; zero arms ran"
}
```

本次只验证了真实进程可启动并响应 health，**没有**为这五个应用建立
Session target adapter，也没有运行 B/C/D/E 四臂与多个 seed。空跑一个
health 探针不能算 `target_reached`，所以：

- 不写 `FREEZE_SS07.json` 成功清单；
- 只写 `FREEZE_REFUSED.json`；
- 每个应用的 `arms_run=0`、`seeds_run=0`；
- 不使用受控 `session_gate` 的结果冒充真实应用结果。

## 根因与下一步

1. 五个应用都有自身的 auth/CSRF/DB 初始化；需要为每个应用建立：
   - 可信 Session observer（读取该应用真实 Cookie/session backend）；
   - 真实 HTTP target adapter；
   - 预注册的 request pool 与 target block；
   - 隔离数据库/账号 fixture。
2. Spirit 当前 checkout 缺少 `test_project`，需先修复其测试/运行环境，
   不能静默跳过。
3. 当前 Atheris 在线调度与四臂协议已在受控 `session_gate` 上通过
   SS-07C/D；接入真实应用前不能声称 SS-07F 成功。

## 反例测试

```text
stateforge/tests/evaluation/test_real_apps.py  2 passed
  - 预注册必须包含五个应用且先于 results；
  - 源码树哈希对内容敏感，忽略缓存。
```
