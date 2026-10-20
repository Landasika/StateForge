# SS-07A Session 四臂实验（物理请求计费）

- protocol: `ss06-v1`
- commit: `9058bb0a29d39a6d4871bb5c9b7ecf2321c05325`
- seeds: `[1]`
- 真实请求预算: `40`
- 静态分析墙钟: `0.02706` 秒
- 目标：`app.py::protected::173-173::block::1`
- 每个 target 调用 = 1 个真实 HTTP 请求；多步输入 = 1 个 sequence、多个请求。bootstrap、验证、前缀重放、隔离对照全部计费。

## 汇总

| arm | target | verified | HTTP | Atheris inputs | bootstrap | validation | prefix replay | suffix | wall(s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| SINGLE | 0/1 | 1/1 | 40.0 | 40.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.583 |
| SEQUENCE | 0/1 | 1/1 | 40.0 | 11.0 | 29.0 | 0.0 | 0.0 | 0.0 | 0.558 |
| STATE_GUIDED | 0/1 | 1/1 | 40.0 | 17.0 | 21.0 | 2.0 | 0.0 | 0.0 | 0.785 |
| STATE_REUSE | 1/1 | 1/1 | 7.0 | 0.0 | 0.0 | 2.0 | 2.0 | 3.0 | 0.032 |

## C vs B 配对（真实请求）

| seed | B HTTP | C HTTP | C-B | B target | C target | D actual HTTP | D end-to-end HTTP |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 40 | 40 | 0 | False | False | 7 | 47 |

## 诚实边界

- 这是小样本功能对照，不宣称统计显著。
- Atheris 输入数与真实 HTTP 请求数分开报告；Atheris 初始化可能多执行输入，原始日志保留。
- D 的实际 HTTP 只计它自己的重放；`end_to_end` 才包含 C 的前缀发现/验证成本，二者不混用。
- D 的 `bootstrap_http_requests` 必须为 0；若没有可信 C 前缀，记录 `NO_CHECKPOINT`，不回退到完整成功链。
- 前缀复用是请求重放，不是物理内存快照。

原始记录：`per_run.jsonl`、各 arm 的 `ledger/per_request.jsonl`、`run_manifest.json`。
