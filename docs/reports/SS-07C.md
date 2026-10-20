# SS-07C 完成报告：Atheris 运行中消费 Session 反馈

## 状态

**已完成并可在受控靶上复现。** 本报告只记录 SS-07C。

```text
Batch: SS-07C
Base commit: 9058bb0
```

## 机制

新增 `stateforge/fuzzing/session_online.py`：

- 候选仍来自现有静态 `SessionAccessIndex` /
  `query_session_frontier`，不新增 fuzzer，不写目标专用规则。
- `OnlineSessionScheduler` 在 **Atheris 启动后**按真实执行反馈工作：
  - handler 每次执行后把 trace 与可信 Session view 送入调度器；
  - 只有 `VERIFIED_TRUE` 才增加 `verified_progress`；
  - 已验证当前焦点或连续无进展时切换候选 writer；
  - 每个决策写 `scheduler_online.jsonl`。
- Atheris `Setup(..., custom_mutator=...)` 注册函数式 mutator：
  - 每次变异先读取当前调度上下文；
  - `mutation_decision` 记录 `mode / writer_block /
    condition_group_id / consumed`；
  - guided 决策作用于后续输入；未消费/异常显式记 `consumed=false`。
- 不新增独立 fuzzer；没有在线 LLM；没有目标专用成功链。

`entry_driver.py` 新增显式开关：

```text
--session-online
--session-online-exploration
```

仅 `feedback/frontier` 可用，且必须提供 target block 与 request pool。
不开启时 `Setup` 仍走原来的两参数路径，旧 A–F 行为不变。

`session_experiment` / `session_ss06` 增加 `--session-online`，只对
`STATE_GUIDED` 生效；`online/online_summary.json` 记录每个 seed 的决策、
切换、变异与消费数。

## 实际在线证据

在 `session_gate` 上，先放入一个合法 C10 seed，再运行 10 个 Atheris 输入：

```bash
python -m stateforge.fuzzing.entry_driver \
  stateforge/targets/session_gate/fuzz_target.py \
  <ir.json> <corpus> <out> \
  --mode feedback --seconds 4 --seed 1 --max-runs 10 \
  --session-online --session-online-exploration 0 \
  --session-target-block 'app.py::protected::173-173::block::1' \
  --session-request-pool <request_pool.json> \
  --session-max-steps 3 --cost-ledger \
  --max-target-http-requests 40 --run-id online-smoke3
```

原始日志摘要：

```text
decisions:           10
frontier_switch:      2
mutations:            8
consumed mutations:   8
state_novelty:        6
guided consumed:      8
```

时间顺序（同一 `scheduler_online.jsonl`）：

```text
atheris-1  state_novelty VERIFIED_TRUE
           frontier_switch authenticate -> clear (verified-progress)
           mutation_decision guided clear, consumed=true

atheris-2  frontier_switch clear -> logout (verified-progress)
           mutation_decision guided logout, consumed=true
后续输入  mutation_decision guided logout, consumed=true ...
```

即：第一个输入执行后产出的新 Session 事实，改变了后续 mutator 的焦点；
第二个输入再次改变焦点，后续 guided 变异消费了新焦点。这不是“启动前
一次性排序”。

40 请求预算的 protocol 冒烟（1 seed）也产生：

```text
STATE_GUIDED online: decisions=17, switches=7, mutations=6,
                     consumed=6, target_reached=0/1
```

该次 C 未在预算内命中 target；原始日志未隐藏失败，D 仍只使用 C 的已独立
验证准备检查点，未使用 C 的成功链。

## 反例测试

```text
stateforge/tests/fuzzing/test_session_online.py  3 passed
  - verified 反馈触发切换，下一变异消费新 writer
  - UNKNOWN / 不可信 view 不触发切换
  - 每个决策均落盘，UNKNOWN 不计 verified progress
```

## 已知限制

- 受控靶的候选 writer 都是同一条 Session 条件的静态 MAY；在线调度证明显
  示的是“反馈改变焦点”，不是“每个 writer 一定可独立执行”。
- 真实 Atheris 的 `-runs` 初始化可能先执行一个空输入；日志保留，不做
  人工截断。
- 当前只支持函数式 custom mutator；若未来 Atheris 版本移除该接口，需要
  走显式分 epoch 回退，不能静默退化为静态排序。
