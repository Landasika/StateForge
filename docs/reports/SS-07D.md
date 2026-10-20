# SS-07D 完成报告：受控回归、在线因果与冻结门禁

## 状态

**受控回归通过，已生成冻结清单。** 本报告只记录 SS-07D。

```text
Batch: SS-07D
Base commit: d502c84 (SS-07C)
Controlled target: stateforge/targets/session_gate
```

## 协议

- 2 seeds（1,2），四个公平臂，每臂相同 80 个物理 HTTP 请求预算：
  - `SINGLE`：单请求；
  - `SEQUENCE`：静态候选写入块序列（无在线反馈）；
  - `STATE_GUIDED`：C，开启 `--session-online`；
  - `STATE_REUSE`：D，不 bootstrap，只重放 C 的独立验证准备检查点。
- 所有臂使用同一 target block、请求池、最大步数、seed 范围。
- C 的在线调度日志与 D 的继承成本分别保存。
- Atheris 初始化的额外空输入保留在原始日志，不裁剪。

## 结果（2 seeds，均值）

| 臂 | 目标命中 | HTTP/seed | bootstrap | 验证 | prefix | suffix | wall(s) |
|---|---:|---:|---:|---:|---:|---:|---:|
| SINGLE | 0/2 | 80.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.914 |
| SEQUENCE | 1/2 | 80.0 | 56.0 | 0.0 | 0.0 | 0.0 | 0.754 |
| STATE_GUIDED | 2/2 | 80.0 | 21.0 | 2.0 | 0.0 | 0.0 | 1.796 |
| STATE_REUSE | 2/2 | **7.0** | **0.0** | 2.0 | 2.0 | 3.0 | 0.033 |

D 的 explicit end-to-end（继承 C 成本 + D 实际请求）：

```text
mean_end_to_end_target_http_requests = 87.0
mean_end_to_end_wall_seconds = 1.83
```

D 的实际请求只有 7，但它的端到端成本必须加上 C 的前缀发现/验证；
报告没有把 7 冒充成完整准备成本。

C 的在线证据（两次 seed）：

```text
consumed_mutations = 39 + 46 = 85
frontier switches   = 7 + 7 = 14
```

每次 switch 后都有 `mutation_decision.mode=guided` 且
`writer_block` 为新焦点、`consumed=true`；这些 decision 发生在 Atheris
第一个输入之后，不是启动前排序。

## 冻结门禁

新增 `stateforge/evaluation/ss07d.py`：

`validate_controlled()` 在冻结前必须全部通过：

1. 四臂 seed 数一致且所有 seed 存在；
2. 每臂实际 HTTP ≤ 80；
3. 每臂 wall-clock > 0；
4. D 的 `bootstrap_http_requests == 0`；
5. D 的成功必须绑定 verified prep 与 checkpoint id；
6. 根级 `per_request.jsonl` 请求数 == `per_run` 总和；
7. C 至少 2 个在线 mutation 被消费，且至少 1 次焦点切换。

任何一项失败：

```text
freeze_refused.json 被写出
raise FreezeRefused
不写 freeze_manifest.json
```

本控制运行的实际校验：

```text
all_arms_equal_seed_count: true
all_seeds_present: true
physical_request_budget: true (over=[])
wall_clock_measured: true (zero=[])
d_no_bootstrap: true (boot=[])
d_checkpoint_bound: true
root_logs_recomputable: true (root=494 per_run=494)
online_consumed_after_start: true (consumed=85 switches=14)
online_feedback_changed_focus: true
```

`freeze_manifest.json` 记录 source revision、协议文件哈希、源码哈希、
测试命令/结果、原始日志路径与完整 validation。

## 反例测试

```text
stateforge/tests/evaluation/test_ss07d.py  7 passed
  - 合法运行可冻结；
  - D bootstrap → 拒绝；
  - C 无消费 decision → 拒绝；
  - 超预算 → 拒绝；
  - D 成功无 checkpoint id → 拒绝；
  - wall=0 → 拒绝；
  - 根日志与 per_run 不一致 → 拒绝。
```

## 已知限制

- “回归”是受控 session_gate 上的四臂一致性/预算/证据门禁，不等于五个真实
  应用都已运行。
- Atheris 的 `-runs` 计数与实际输入次数可能因初始化差 1；以 HTTP ledger
  为准，原始日志保留。
- D 的复用是 HTTP 重放，不是物理内存快照。
