# Session-First 实验报告：BASE / GUIDED / REUSE

## 实验想回答什么

三个问题：

1. 只给独立单请求种子，默认 Fuzzer 能不能自己组合出“先登录、再访问
   受保护页面”？
2. 加上 Session 定向搜索，能不能稳定找到？
3. 把已验证的检查点前缀直接放进语料，能不能复用？

## 怎么做的

- 靶：`stateforge/targets/session_gate`，Flask 签名 Cookie Session。
- 目标块：`app.py::protected::173-173::block::1`。
- 请求池：9 个独立单请求，包含登录成功/失败、4 个受保护变体、noise、
  logout、clear。
- 请求池里没有完整序列。
- 3 个 seed：`1, 2, 3`。
- 每个组、每个 seed 跑 2 秒 Atheris。
- 三组都使用 `--mode feedback --lifecycle reset --observe-state`。

三组的区别只有种子来源：

| 组 | 语料里有什么 |
|---|---|
| BASE | 9 个单请求 C10 种子 |
| SESSION-GUIDED | 同样的单请求 + 启动前定向搜索写出的种子 |
| SESSION-REUSE | 同样的单请求 + 已验证检查点前缀 |

## 结果

| 组 | 目标到达 | 写入块覆盖 | Session 已验证 | 平均总执行 |
|---|---:|---:|---:|---:|
| BASE | 0/3 | 3/3 | 3/3 | 469.7 |
| SESSION-GUIDED | 3/3 | 3/3 | 3/3 | 486.3 |
| SESSION-REUSE | 3/3 | 3/3 | 3/3 | 459.7 |

逐 seed：

| 组 | seed | 目标 | 验证到的 Session 事实 | 总执行 | 搜索状态 |
|---|---:|---:|---|---:|---|
| BASE | 1 | 否 | `Session.other`, `Session.uid` | 434 | — |
| SESSION-GUIDED | 1 | 是 | `Session.other`, `Session.uid` | 487 | VERIFIED |
| SESSION-REUSE | 1 | 是 | `Session.other`, `Session.uid` | 447 | — |
| BASE | 2 | 否 | `Session.other`, `Session.uid` | 466 | — |
| SESSION-GUIDED | 2 | 是 | `Session.other`, `Session.uid` | 478 | VERIFIED |
| SESSION-REUSE | 2 | 是 | `Session.other`, `Session.uid` | 448 | — |
| BASE | 3 | 否 | `Session.other`, `Session.uid` | 509 | — |
| SESSION-GUIDED | 3 | 是 | `Session.other`, `Session.uid` | 494 | VERIFIED |
| SESSION-REUSE | 3 | 是 | `Session.other`, `Session.uid` | 484 | — |

## 怎么理解 BASE 的 3/3

BASE 的“Session 已验证 3/3”看起来有点怪：目标都没到，怎么会有已验证事实？

原因是请求池里有 `POST /authenticate` 这个单请求。它确实执行了
`session["uid"] = 7`，运行时也确实能从 Cookie 里读回 `uid=7`。

所以 BASE 有：

- 写入块覆盖：3/3；
- Session 事实：3/3；
- 目标到达：0/3。

这正好是这次要证明的分离：**写入块覆盖和会话事实，都不等于目标到达。**

## 检查点

从 GUIDED 的结果里创建检查点：

```text
sc-49405742647b
  POST /authenticate {"passcode": "valid"}
  GET  /protected
```

冷启动重放：`REPLAY_VERIFIED`，Session 值 `{"uid": 7}`。

不带前缀、只跑后缀 `GET /protected`：目标不到，隔离对照通过。

## 静态查询

- 9 个 Session 访问事件；
- 9 个都是 `STATIC_MAY`；
- 0 个 UNKNOWN；
- 候选写入块：`authenticate`、`logout`、`clear`。

## 回归

- 全量：`731 passed, 1 skipped, 26 deselected, 1 warning in 660.46s`；
- 相对基线 656，新增 75 个测试；
- 架构边界：`5 passed`；
- `micro_benchmark`：静态 21 个用例和动态 5 个用例全部 1.00。

## 别过度解读

- 只有 3 个 seed、2 秒、一个微型靶，不能拿来说统计显著。
- GUIDED 的总执行数包含搜索预算，不能简单说“比 BASE 快”。
- 这个实验证明的是：在同样请求池和隔离条件下，定向搜索和检查点复用
  能稳定产出可重放的两步序列。
- 换成更大的请求池、更多 target、更长预算，结论可能不同，需要重新测。

## 产物在哪

- `artifacts/session_first/session_experiment.json`
- `artifacts/session_first/SESSION_EXPERIMENT.md`
- `artifacts/session_first/raw/`
- `artifacts/session_first/checkpoints/`
