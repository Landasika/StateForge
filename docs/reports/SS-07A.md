# SS-07A 完成报告：真实请求计费与公平四臂修复

## 状态

**已完成并提交。** 本报告只记录 SS-07A；SS-07B/C/D/E/F 尚未开始。

```text
Batch: SS-07A
Git commit: cf0c495
```

## 编码前基线

```text
git rev-parse HEAD
f415d963399d52c855475533d6e72b3ef16dc904

git status --short
?? 1.md
?? artifacts/phase1/
?? artifacts/phase2/
?? artifacts/phase4/
?? artifacts/session_first/
?? artifacts/session_ss06/raw/
?? exp/
?? plan.md

python -V
Python 3.13.5

python -m pytest stateforge/tests/state/ stateforge/tests/fuzzing/ stateforge/tests/runtime/ -q
317 passed, 1 warning
```

环境：

```text
Linux x86_64, 8 CPU
Flask 3.1.0, Werkzeug 3.1.3, Atheris 3.1.0, pytest 8.3.4
Django 未安装（SS-07F 前需要单独准备 Django 环境）
```

## 改动

### 1. 统一物理请求边界

新增 `stateforge/runtime/execution_costs.py`：

- `ExecutionCosts`：请求、序列、Atheris 输入、bootstrap、验证、前缀重放、
  后缀、observer、reset、调度、wall-clock、analysis 时间。
- `ExecutionCostLedger`：在 `target` 可调用对象外只包一层；每次真实调用
  记一个 HTTP 请求，异常也记。
- 多步 C10 输入：一个 `sequence_start`，每个 step 一个 `request` 事件。
- 硬预算：超过 `request_limit` 时在 target 调用前抛 `BudgetExceeded`，
  写 `budget_blocked` 事件，不执行超限请求。
- 增量 JSONL + `cost_snapshot.json`；`read_costs()` 在快照缺失时从
  `per_request.jsonl` / `per_sequence.jsonl` 重算。
- 所有 wall-clock 使用 `time.monotonic()`；没有硬编码 0 的分支。

`entry_driver.py` 新增显式开关：

```text
--cost-ledger
--max-target-http-requests
--cost-out
--run-id
--arm
--target-block
```

默认不开启，旧 A–F / SS-06 行为保持不变。

`session_experiment._run_driver()` 增加计费参数，并把 child ledger 结果
带回父进程。

### 2. 物理请求数替代语料长度估算

`session_ss06.py` 不再使用：

```python
sum(s["length"] for s in raw["corpus_sequences"])
```

作为 HTTP 数。它现在读取 child ledger 的：

```text
target_http_requests
sequence_attempts
atheris_inputs
bootstrap_http_requests
checkpoint_validation_http_requests
prefix_replay_http_requests
suffix_http_requests
observer_calls
reset_calls
elapsed_wall_seconds
```

旧字段 `bootstrap_execs / atheris_execs / total_target_execs` 仅作为
兼容别名，值来自真实计数，不再由语料长度或输入数冒充。

### 3. D 组不再 bootstrap

`STATE_REUSE` 分支现在：

- 不调用 `_bootstrap()`；
- 只从同一 seed 的 `STATE_GUIDED` 上下文读取已经独立验证的准备检查点；
- 每个后缀从冷启动重放前缀；
- 记录两套成本：
  - `execution_costs`：D 自己的真实重放请求；
  - `shared_preprocessing_costs`：C 的 bootstrap/验证成本；
  - `end_to_end_costs`：二者相加，单独报告。
- 无检查点时记录 `NO_CHECKPOINT`，不回退到完整成功链。
- `bootstrap_http_requests` 必须为 0。

### 4. 验证前缀真实计费

`replay_session_checkpoint()` 新增可选 `cost_ledger`：

- 前缀单独验证：`checkpoint` phase；
- prefix + suffix：`prefix_replay` / `suffix` phase；
- target witness：单独 sequence；
- reset/observer 由 ledger 包装一次，不再由调用方重复包装。

`STATE_GUIDED` 的检查点验证不再用 `prefix_len` 估算；它执行一次真实的
冷启动前缀重放，只有 `preparation_verified=True` 才把前缀交给 D。

### 5. 报告产物

一次 protocol run 现在输出：

```text
run_manifest.json
per_request.jsonl        # 根级聚合，父搜索 + child Atheris
per_sequence.jsonl
scheduler.jsonl
per_run.jsonl
summary.json
REPORT.md
SESSION_SS06_REPORT.md
raw/<seed>/<arm>/out/ledger/...
raw/<seed>/<arm>/out/atheris_cost/...
```

## 反例测试

新增 `stateforge/tests/runtime/test_execution_costs.py`：

1. 3 步输入 → 3 个 HTTP、1 个 Atheris 输入；
2. 403 与 target 抛异常都计执行；
3. 硬预算在调用前停止，不执行超限请求；
4. phase 分项相加；observer / reset / scheduler 独立计数；
5. 固定 mock 时钟下，逐项汇总与原始事件一致。

新增 `stateforge/tests/evaluation/test_session_ss07a.py`：

1. D 组不调用 `_bootstrap()`；D 的 `bootstrap_http_requests == 0`；
2. D 的 end-to-end 只作为继承成本单独字段，不冒充 D 实际请求；
3. 每个 arm 的 `elapsed_wall_seconds > 0`；
4. 每个 arm 不超过硬请求预算；
5. 从 `per_request.jsonl` 重算的数量与 `per_run` 完全一致；
6. 真实 Atheris 下，3 步 C10 种子由一个 sequence 产生 3 个请求，不是
   3 个 Atheris 输入。

## 执行命令

```bash
python -m pytest \
  stateforge/tests/runtime/test_execution_costs.py \
  stateforge/tests/evaluation/test_session_ss07a.py \
  stateforge/tests/evaluation/test_session_protocol.py -q

python -m pytest stateforge/tests/state/ stateforge/tests/fuzzing/ \
  stateforge/tests/runtime/ -q

python -m pytest -q
```

结果：

```text
8 passed  # ledger + SS-07A 集成测试
764 passed, 1 skipped, 26 deselected, 1 warning  # 全量
```

三 seed 冒烟证据（80 请求预算，1 秒）：

```text
SINGLE       target 0/3  HTTP 80.0  Atheris 80.0  bootstrap 0.0
SEQUENCE     target 2/3  HTTP 80.0  Atheris 29.7  bootstrap 50.3
STATE_GUIDED target 3/3  HTTP 80.0  Atheris 57.0  bootstrap 21.0  validation 2.0
STATE_REUSE  target 3/3  HTTP 7.0   Atheris 0.0   bootstrap 0.0   validation 2.0  prefix 2.0  suffix 3.0
```

根级 `per_request.jsonl` 与 `per_run.jsonl` 的请求总数均为 `741`，可逐条
重算。D 的 `bootstrap_http_requests=0`，其 `end_to_end` 成本单独记录。

## 已知限制

- 当前 Atheris `-runs` 初始化可能先执行一个空输入；原始日志保留，未裁掉。
- D 的 end-to-end 成本包含 C 的 bootstrap/Atheris；D 的实际请求只含重放，
  两者分开报告，不能直接得出“D 更快”。
- 目前只完成计费与公平口径修复；在线 Session 引导（SS-07C）尚未实现。
