# StateForge V0.2：统一状态执行入口（PR-0～PR-3）

本批交付把既有静态分析、状态推导、请求规划、轨迹执行、检查点和 Atheris 连成一条主流程。主流程先在微靶 `t_state_shop.py` 上验收，再原样用于真实 Django 应用 linkding（见「真实 Web 应用闭环」一节）。PR-4 的统一变异调度与 PR-5 的反馈重规划属于后续工作。

## 运行

在仓库根目录运行。PR-0、规划、执行和冷启动重放只需基础安装；Fuzz 模式另需 `pip install -e '.[fuzz]'`。

```bash
# PR-0：零手写请求的独立冒烟测试
python scripts/state_goal_smoke.py

# PR-1、PR-2：分析→推导→规划→执行→验证→独立进程重放→保存检查点
python -m stateforge.cli goal-run stateforge/targets/stateful \
  --source t_state_shop.py \
  --block 't_state_shop.py::_handle::91-92::block::1' \
  --mode execute --out results/goal-run

# PR-3：完成上述步骤后注入 C10 种子，再启动真实 Atheris
python -m stateforge.cli goal-run stateforge/targets/stateful \
  --source t_state_shop.py \
  --block 't_state_shop.py::_handle::91-92::block::1' \
  --mode fuzz --fuzz-seconds 2 --fuzz-runs 100 \
  --out results/goal-fuzz
```

自动生成并执行 `register → create_order → ship_order`，三步共享一个模块内的 Session 和数据库。Fuzz 输入边界使用 `reset_state()`，一个输入内不会在请求之间重置状态。原有 Atheris 驱动仍保持默认 `noop` 行为，新主流程显式传入 `--lifecycle reset`。

四种模式：

| 模式 | 行为 | 成功判据 |
|---|---|---|
| `plan` | 只分析、推导、规划；不导入执行目标模块 | 生成非空计划，`status=planned`，`verified=false` |
| `execute` | 执行生成的链，再在独立 Python 解释器中重放 | 每步写块证据有效、无异常、目标块命中，冷重放也成功 |
| `replay` | 从 `--checkpoint` 读取请求链，不调用规划器 | 独立进程中的同一请求链重新命中目标 |
| `fuzz` | 执行、冷重放、保存检查点、注入 C10、启动 Atheris | 真实输入回调消费精确种子、完成全部请求、隔离有效且命中目标 |

冷启动重放可以直接读取上一次报告中的检查点路径：

```python
import json
from stateforge.pipeline import GoalRunner

checkpoint = json.load(open("results/goal-run/summary.json"))["checkpoint"]
report = GoalRunner().run(
    "stateforge/targets/stateful",
    "t_state_shop.py::_handle::91-92::block::1",
    source="t_state_shop.py", mode="replay", checkpoint=checkpoint,
    out="results/goal-replay",
)
assert report.success, report.summary
```

CLI 同样支持 `--mode replay --checkpoint <上述 checkpoint 路径>`。检查点记录分析时的源码指纹；版本不匹配时拒绝重放。指纹沿用 ICFG 的路径、大小、修改时间机制，不是内容签名，也不支持跨机器搬迁后保持原路径不变的承诺。

## 产物与失败诊断

固定五个文件都属于当前一次运行。输出目录复用时先清空这五个报告中的旧成功状态；历史执行证据保留在各自的 `runs/<run_id>/` 目录，当前报告只引用本次结果。

```text
results/goal-fuzz/
├── ir.json                        # 原有 ProgramIR 格式
├── goal.json                      # StateGoal 约束、守卫与推导依据
├── plan.json                      # 原有 PlanSegment 格式
├── execution.json                 # 每步请求、写块、覆盖、阻塞原因
├── summary.json                   # 当前运行阶段、总体状态、证据路径
└── runs/<run_id>/
    ├── ir.json                    # 本次独立进程共用的 IR
    ├── cold_replay.json            # 子进程 PID、退出状态、完整执行证据
    ├── checkpoints/gc-….json       # 仅冷重放成功后保存
    ├── corpus/<content_id>         # C10 序列种子；Fuzz 模式产生
    ├── goal_spool/                 # 复用既有种子发布与去重协议
    └── fuzz/
        ├── atheris.log
        ├── input_receipts.jsonl    # 匹配原始种子 SHA-256 的输入回执
        ├── summary.json
        └── …                      # 既有覆盖反馈、候选、采样产物
```

未运行的固定产物写为 `{"status": "not_run", "failed_stage": ...}`。失败后的 `summary.status=failed`，`failed_stage` 定位到 `validation`、`analysis`、`state_extraction`、`goal_derivation`、`planning`、`execution`、`verification`、`checkpoint_loading`、`checkpoint`、`cold_replay` 或 `fuzzing`，并记录异常类型和消息。

`target_covered` 表示主执行轨迹包含目标块；`execution_verified` 表示这条完整链的每步证据均通过；`verified` 表示请求模式要求的全部验证已完成。即使后续 Fuzzer 启动失败，已保存的有效检查点仍可保留，但总体状态失败、`verified=false`。CLI 对成功返回 0，执行或验证失败返回 1，参数、分析及输出目录错误返回 2。

Atheris 验收核对运行 ID、种子 SHA-256、序列完成步数、生命周期、目标块命中、无执行异常和引擎退出状态。仅存在 corpus 文件不会被判为消费成功。回执逐行写入，适应 libFuzzer 使用 `_Exit` 退出。

验证依据是运行轨迹中的目标块与预期写块。它证明该目标路径可执行，不等同于对任意 Session、数据库状态值做完整求值。目标的 `HITS` 仅作为辅助证据。

## 模块边界

`pipeline` 位于 L3.5，由 CLI 调用，依赖既有的 analysis、state、runtime、solving、fuzzing。`GoalRunner` 不实现新规划算法，复用 `derive_state_goal()`、`plan_for_block()`、`execute_plan()`；执行公共加载入口为 `load_target_module()`，旧 `_load_target_module` 名称仍兼容。`verify_plan_execution()` 由运行时提供，检查点和主流程共用同一验证规则。

`solving.checkpoint_replay` 负责独立解释器重放；`pipeline.goal_fuzz` 负责种子发布、子进程启动和回执验收；`fuzzing.input_receipts` 只负责输入回调证据落盘。驱动与底层模块不反向依赖 pipeline。动态子进程入口已登记到架构文档并受边界测试约束。

## 验收进度

| V0.2 验收项 | 本批结果 |
|---|---|
| 一条命令完成目标分析、状态推断与规划 | 完成 |
| 自动生成多步请求序列 | 完成 |
| 执行序列并验证目标块命中 | 完成 |
| 成功链保存检查点并冷启动重放 | 完成 |
| 检查点转换 C10 种子并被真实 Atheris 消费 | 完成 |
| 基础、字典、指导变异统一调度 | PR-4 待实施 |
| 失败反馈给规划器并调整计划 | PR-5 待实施；本批提供失败证据 |
| 至少一个真实 Web 应用闭环验证 | 完成（linkding，见下节） |

当前为 **6/8**，本批范围是 PR-0～PR-3，另加一次真实 Web 应用闭环。实际 Atheris 消费以引擎输入初始化时执行原始种子为验收，短程运行不构成变异收益的实验结论。

## 真实 Web 应用闭环：linkding

`app-goal-run` 是同一主流程对真实应用的第二入口：分析走应用桥接，执行走应用自己的解释器与测试客户端，冷启动重放要求「新进程 + 新未登录客户端」。四种模式与 `goal-run` 同名同义。

```bash
external/linkding/.venv/bin/python -m stateforge.cli app-goal-run external/linkding \
  --block 'bookmarks.py::edit::239-241::block::1' \
  --analysis-root external/linkding/bookmarks/views \
  --harness stateforge.targets.apps20.linkding.harness \
  --adapter stateforge/targets/real_apps/linkding/inproc_target.py \
  --mode fuzz --fuzz-seconds 2 --fuzz-runs 30 --out results/app-goal-run
```

两点与微靶不同，都是架构事实而非配置便利：

- **解释器**：命令必须用应用自己的 venv 运行。静态分析与状态推导不依赖 Django，但执行阶段要 import 应用模块；`--python` / `SF_APP_PYTHON` 指定冷重放与 Atheris 子进程所用的解释器。
- **分析面收窄**：`--analysis-root` 必须收窄到 `bookmarks/views`。以整个应用为轨迹根时单次映射约 13 秒，无法支撑 fuzzing；收窄后约 1.5 秒。

证据（`results/app-goal-run/summary.json`）：

| 环节 | 证据 |
|---|---|
| 目标约束 | `Session.authed_as` truthy（`@login_required` 守卫） |
| 计划 | `POST /login/` → `GET bookmarks/<int:bookmark_id>/edit` |
| 执行 | 302 → 200，目标块命中，`blocking` 为空 |
| 前缀必要性 | 同一客户端只发末步 → 302，目标块未命中 |
| 冷启动重放 | 独立进程，`parent_pid` 为运行器，整链仍命中 |
| C10 种子 | 2 步，被真实 Atheris 在 input callback 中完整执行 |
| 输入回执 | `engine=atheris`、`lifecycle=reset`、`isolation=reset`、`target_covered=true`、`error=null` |

冷进程对照（`stateforge.solving.http_checkpoint_replay`）：整链 `302→200` 命中；仅末步 `302` 不命中。

## 测试

```bash
python -m pytest \
  stateforge/tests/integration/test_goal_runner.py \
  stateforge/tests/integration/test_goal_fuzz.py \
  stateforge/tests/state/test_plan_executor.py \
  stateforge/tests/solving/test_goal_checkpoint.py \
  stateforge/tests/fuzzing/test_sequence_seeds.py \
  stateforge/tests/fuzzing/test_lifecycle_wiring.py \
  stateforge/tests/test_architecture_boundaries.py \
  stateforge/tests/integration/test_app_goal_runner.py \
  stateforge/tests/runtime/test_http_plan_executor.py -q

# 需要 linkding venv（未安装则整组跳过）
python -m pytest stateforge/tests/evaluation/test_real_app_goal.py -q

# 基础 wheel 在隔离环境中也能执行 GoalRunner 和冷重放，不安装 Atheris/HTTP/Web 依赖
python -m stateforge.evaluation.check_distribution
```

集成测试覆盖正确链、计划模式、阶段错误、异常发生在目标命中之后、残缺检查点、源码指纹不匹配、独立进程 PID、嵌套源目录映射、真实 Atheris 消费，以及序列共享状态而输入间隔离。真实引擎测试在未安装 Atheris 的环境中明确跳过。

真实应用侧另有负向守卫：登录不建立会话时判失败、目标块无需前缀即可命中时判失败（不能算状态构造证据）、`plan` 模式不得执行、`fuzz` 缺 `--adapter` 时报配置错误且保留已通过的检查点、HTTP 检查点不得被当作微靶检查点执行。

## 本次验收记录（2026-11-04）

- PR-0 脚本输出三步链及 `target_covered: True`；最初指定的两个专项测试文件共 12 项通过，随后增加了一项检查点失败证据回归测试。
- 全量 pytest 在屏蔽 LLM 凭据后运行：**928 passed, 1 skipped, 26 deselected, 1 warning**，耗时 639.94 秒；日志 `/tmp/stateforge-v02-pytest.log`。warning 为既有 Pydantic `PlanStep.json` 字段命名警告。
- `execute`、`replay`、`fuzz` 命令均成功，报告分别位于 `results/goal-run/summary.json`、`results/goal-replay/summary.json`、`results/goal-fuzz/summary.json`。
- Atheris 实跑 100 次输入，原始 C10 种子 SHA-256 为 `1e4ec72e03ce5e3bd0de23ec34a9c36392889085034cd680b2bd4ac6ebda10ab`；输入回执显示完成 3 步、reset 隔离、目标块覆盖、无异常。
- wheel 在独立虚拟环境安装后，基础依赖即可完成 GoalRunner 执行及独立进程重放；不安装 Atheris、httpx、Flask。日志 `/tmp/stateforge-v02-wheel.log`。
- 微基准 21 项静态指标与 5 项动态轨迹准确率均为 1.00；`git diff --check` 通过。

## 真实应用验收记录（2026-11-05）

- `app-goal-run` 在 linkding 上四种模式全部成功；完整 fuzz 链路（含冷重放）单次约 21 秒。
- 全量 pytest：**944 passed, 1 skipped, 26 deselected**，耗时 708.49 秒；linkding 端到端 5 项通过，耗时 72.96 秒。
- `execute` 模式阶段全绿：`validation / analysis / state_extraction / goal_derivation / planning / execution / isolation / cold_replay / checkpoint`。
- 冷重放进程与运行器 PID 不同，`parent_pid` 指向运行器；独立重跑 `http_checkpoint_replay` 得到整链 `302→200` 命中、仅末步 `302` 不命中。
- Atheris 实跑 30 次输入，C10 种子 SHA-256 `0c353bc094e11050ba0aa4ec3a4b437ee8afd8861eab0d73d439c7d415073f80`，回执记录完成 2 步、`lifecycle=reset`、`isolation=reset`、目标块覆盖、无异常。
- 证据目录 `results/app-goal-run/`（五份清单 + `runs/<id>/` 下的检查点、冷重放记录、种子回执与 Atheris 日志），共 10 个 JSON 产物。
