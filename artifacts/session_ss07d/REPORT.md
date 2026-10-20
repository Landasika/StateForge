# SS-07A Session 四臂实验（物理请求计费）

- protocol: `ss06-v1`
- commit: `d502c84e06081a416d18611464539ee9d47d90c4`
- seeds: `[1, 2]`
- 真实请求预算: `80`
- 静态分析墙钟: `0.025269` 秒
- 目标：`app.py::protected::173-173::block::1`
- 每个 target 调用 = 1 个真实 HTTP 请求；多步输入 = 1 个 sequence、多个请求。bootstrap、验证、前缀重放、隔离对照全部计费。

## 汇总

| arm | target | verified | HTTP | Atheris inputs | bootstrap | validation | prefix replay | suffix | wall(s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| SINGLE | 0/2 | 2/2 | 80.0 | 80.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.914 |
| SEQUENCE | 1/2 | 2/2 | 80.0 | 24.0 | 56.0 | 0.0 | 0.0 | 0.0 | 0.754 |
| STATE_GUIDED | 2/2 | 2/2 | 80.0 | 60.5 | 21.0 | 2.0 | 0.0 | 0.0 | 1.796 |
| STATE_REUSE | 2/2 | 2/2 | 7.0 | 0.0 | 0.0 | 2.0 | 2.0 | 3.0 | 0.033 |

## C vs B 配对（真实请求）

| seed | B HTTP | C HTTP | C-B | B target | C target | D actual HTTP | D end-to-end HTTP |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 80 | 80 | 0 | False | True | 7 | 87 |
| 2 | 80 | 80 | 0 | True | True | 7 | 87 |

## 诚实边界

- 这是小样本功能对照，不宣称统计显著。
- Atheris 输入数与真实 HTTP 请求数分开报告；Atheris 初始化可能多执行输入，原始日志保留。
- D 的实际 HTTP 只计它自己的重放；`end_to_end` 才包含 C 的前缀发现/验证成本，二者不混用。
- D 的 `bootstrap_http_requests` 必须为 0；若没有可信 C 前缀，记录 `NO_CHECKPOINT`，不回退到完整成功链。
- 前缀复用是请求重放，不是物理内存快照。

原始记录：`per_run.jsonl`、各 arm 的 `ledger/per_request.jsonl`、`run_manifest.json`。
