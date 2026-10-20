# SS-06 Session 四臂消融实验

- protocol: `ss06-v1`
- commit: `3b69923e85e52b22586b6aea8195db162ff3ebdd`
- seeds: `[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]`
- 目标：`app.py::protected::173-173::block::1`
- 预算、靶、请求池、最大步数、reset 规则与随机种子在各臂一致；bootstrap、检查点验证、前缀重放都计入。

## 汇总

| arm | target | verified | mean total | mean Atheris | mean bootstrap | mean validation | mean prefix replay | mean wall(s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| SINGLE | 0/20 | 20/20 | 80.0 | 80.0 | 0.0 | 0.0 | 0.0 | 0.815 |
| SEQUENCE | 8/20 | 20/20 | 80.0 | 45.9 | 34.1 | 0.0 | 0.0 | 0.608 |
| STATE_GUIDED | 20/20 | 20/20 | 81.0 | 65.0 | 15.0 | 1.0 | 0.0 | 0.74 |
| STATE_REUSE | 20/20 | 20/20 | 20.0 | 0.0 | 15.0 | 1.0 | 2.0 | 0.0 |

## C vs B 配对

| seed | C-B total execs | B target | C target | B verified | C verified |
|---:|---:|---:|---:|---:|---:|
| 1 | 1 | False | True | True | True |
| 2 | 1 | True | True | True | True |
| 3 | 1 | True | True | True | True |
| 4 | 1 | False | True | True | True |
| 5 | 1 | False | True | True | True |
| 6 | 1 | False | True | True | True |
| 7 | 1 | True | True | True | True |
| 8 | 1 | False | True | True | True |
| 9 | 1 | True | True | True | True |
| 10 | 1 | False | True | True | True |
| 11 | 1 | True | True | True | True |
| 12 | 1 | False | True | True | True |
| 13 | 1 | True | True | True | True |
| 14 | 1 | True | True | True | True |
| 15 | 1 | False | True | True | True |
| 16 | 1 | False | True | True | True |
| 17 | 1 | False | True | True | True |
| 18 | 1 | True | True | True | True |
| 19 | 1 | False | True | True | True |
| 20 | 1 | False | True | True | True |

## 诚实边界

- 这是一个小样本功能对照，不宣称统计显著。
- Atheris 统计的是输入次数，不是 HTTP 请求数；`http_requests_executed` 只能由语料长度做下界估计。
- STATE_REUSE 的前缀是请求计划，不是物理内存快照；每个后缀仍会重新执行前缀，成本已计入。
- STATE_REUSE 的 total 是状态准备前缀的发现/验证 + 两个后缀的成本，不包含 STATE_GUIDED 中为目标单独跑的 Atheris；它回答的是“准备前缀能否复用”，不是“D 比 C 快”。
- 如果 C 没有优于 B，报告不推断“静态图无价值”，也可能只是靶太简单。

原始记录：`per_run.jsonl`、`run_manifest.json`、`raw/`。
