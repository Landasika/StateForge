# Session-First 实验报告：BASE / GUIDED / REUSE

## 设置

- 靶：`stateforge/targets/session_gate`（Flask 签名 Cookie Session）
- 目标块：`app.py::protected::173-173::block::1`
- 请求池：9 个独立单请求（登录成功/失败、4 个受保护变体、noise、
  logout、clear）；**不含**完整序列
- seeds：`1,2,3`
- 每臂每 seed：2 秒 Atheris + 相同 `--mode feedback --lifecycle reset
  --observe-state`
- BASE：语料只含 9 个单请求 C10 种子
- GUIDED：相同语料 + `--session-guided` 启动前有界搜索
- REUSE：相同语料 + 已验证检查点前缀种子，无搜索
- 产物：`artifacts/session_first/`（`session_experiment.json`、
  `raw/`、`SESSION_EXPERIMENT.md`）

## 汇总

| arm | target reached | writer covered | verified session | proxy-only | mean total exec | mean Atheris exec |
|---|---:|---:|---:|---:|---:|---:|
| BASE | 0/3 | 3/3 | 3/3 | 0/3 | 476.3 | 476.3 |
| SESSION-GUIDED | 3/3 | 3/3 | 3/3 | 0/3 | 487.7 | 472.7 |
| SESSION-REUSE | 3/3 | 3/3 | 3/3 | 0/3 | 459.3 | 459.3 |

逐 seed：

| arm | seed | target | verified atoms | total exec | guided status |
|---|---:|---:|---|---:|---|
| BASE | 1 | False | `Session.other`, `Session.uid` | 463 | — |
| SESSION-GUIDED | 1 | True | `Session.other`, `Session.uid` | 477 | VERIFIED |
| SESSION-REUSE | 1 | True | `Session.other`, `Session.uid` | 442 | — |
| BASE | 2 | False | `Session.other`, `Session.uid` | 449 | — |
| SESSION-GUIDED | 2 | True | `Session.other`, `Session.uid` | 516 | VERIFIED |
| SESSION-REUSE | 2 | True | `Session.other`, `Session.uid` | 481 | — |
| BASE | 3 | False | `Session.other`, `Session.uid` | 517 | — |
| SESSION-GUIDED | 3 | True | `Session.other`, `Session.uid` | 470 | VERIFIED |
| SESSION-REUSE | 3 | True | `Session.other`, `Session.uid` | 455 | — |

## 解释

- **BASE 的 writer/verified 不是目标成功。** 单请求 `authenticate` 种子
  会执行 `session['uid']=7`，因此 writer 块和 `Session.uid` 运行时事实
  存在；但 BASE 没有组合出后续 `GET /protected`，目标块 0/3。
  这正是“写块覆盖/状态事实 ≠ 目标到达”的分离。
- **GUIDED/REUSE 3/3 到达目标**，且 `global_state` 同时记录
  `Session.uid{client-0@session}`（`observed_value=7`, `NUMBER`）。
- 定向搜索每次约 15 次执行发现序列；随后 Atheris 正常继续。GUIDED 的
  mean total 包含搜索预算，因此不宣称“总执行更少”；它证明的是“在相同
  请求池和隔离下能稳定产出可重放的两步 C10 种子”。
- 检查点 replay：`REPLAY_VERIFIED`，`values={'uid': 7}`；无前缀后缀
  隔离对照为 `True`（后缀单独执行不达目标）。
- 静态查询：9 个访问事件全部 `STATIC_MAY`，0 个 UNKNOWN；候选写入块
  为 `authenticate`、`logout`、`clear`。

## 诚实的限制

- 3 个 seed、2 秒、单一微型靶，不构成统计显著性结论。
- 本实验不证明 GUIDED 比强 baseline 更快；BASE 是“只有独立单请求种子”
  的默认行为，未加入人工组合 seed。
- `mean sequence length 1.12` 是语料中 9 个单步 + 1 个两步的平均，不是
  发现序列长度；发现序列固定为 2 步。
- 性能/泛化结论需要更大请求池、更多 target 和更长预算；本轮只报告
  功能性证据与负结果边界。

## 回归与基准

- 全量回归：`731 passed, 1 skipped, 26 deselected, 1 warning in 660.46s`
  （2026-10-20，`python -m pytest -q`；相对基线 656 增加 75 个测试）。
- 架构边界：`python -m pytest stateforge/tests/test_architecture_boundaries.py -q`
  → `5 passed`。
- 微型结构基准：`python -m stateforge.evaluation.micro_benchmark --out /tmp/sf_micro_metrics.json`
  → `exit=0`，21 个静态用例 CFG/CALL/RETURN precision/recall 全 1.00，
  5 个动态 trace 用例 accuracy 1.00。
