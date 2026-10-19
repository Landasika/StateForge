# StateForge 架构审计（Phase 0）

> 审计基线：git `63f8a23`（"阶段1 P0-A/P0-B:输入级状态隔离 + 运行时状态观测"）。
> 测试基线：`python -m pytest stateforge/tests/ -q` → **541 passed, 1 skipped, 26 deselected**（全量，约 7 分钟）。
> 本文只陈述经逐文件核实的既成事实；"缺口"一节明确区分 *未实现* 与 *已实现但未接线*。行号以审计时工作树为准。

---

## 0. 执行摘要

StateForge 已经具备一条完整的 **"静态 SSTG → 反向规划 → C10 序列种子注入 → libFuzzer 执行 → 覆盖代理判达"** 闭环（E/F 臂，`policy=state`），以及一套刚落地的 **输入级状态隔离（P0-A）与运行时状态观测（P0-B）** 契约。但两条线**尚未交汇**：

1. **状态证据不参与任何种子决策**：`StateEvidenceLedger.record()` 的返回值（新 verified 事实）在两个 Collector 里被丢弃（`candidates.py:87`、`frontier_collector.py:74`）——种子保留仍 100% 由代码覆盖/前沿距离决定。
2. **运行时观测只在输入边界发生一次**：`ExecutionContext.finish_run` 是每输入一调（`candidates.py:82`），C10 序列内部的逐请求 before/after 迁移不可见；`TransitionRecorder`（M2 动态边）**只存在于单测**，未接入任何执行路径。
3. **实验协议下 P0-A/P0-B 完全不生效**：`epoch._run_driver`（`epoch.py:28-39`）不传 `--lifecycle` / `--observe-state`，A–F 九组的全部子进程驱动都以 noop 生命周期 + 无观测运行。verified 证据路径只有 CLI 手动开启才存在。
4. **三套状态词表并存且互不映射**：静态 SSTG 原子（`"Order.status=PAID"`）、M1 抽象状态键（`session.role=EQ(admin)|...`）、P0-B StateKey（`Predicate{Entity@Scope}`）。判达（覆盖代理）与证据（verified 台账）分记，但没有任何一侧能把另一侧的词翻译过来——Phase 2/3 的前置条件。
5. **规划闭环缺失败记忆与成功回注**：注入的序列种子若执行失败（401/404），下轮同一未达原子会重新规划出同一条链（内容哈希去重挡住重复写入，但失败原因不入账、无计划签名、无尝试历史）；成功判达的链不会主动回注语料（离线的 `goal_directed_eval` 路径有 checkpoint 回注，fuzz 主循环没有）。

这五个断点即 Phase 1–3 的主要工作量所在；本文后续各节给出文件/函数级定位。

---

## 1. 真实调用链（逐条核实）

### 1.1 单请求 fuzz 输入（libFuzzer handler → 落盘）

```text
atheris.Setup(argv, handler)                      # entry_driver.py:239
└─ handler(data)                                   # entry_driver.py:132/214/225
   └─ collector.handle(dispatch, data)             # candidates.py:62 / frontier_collector.py:51
      ├─ start = len(session.events)               # 事件窗口起点
      ├─ with session.request():                   # tracer.py:82 —— 只切事件归属,不停止收集
      │  ├─ execution.begin_run()                  # execution_context.py:268
      │  │  └─ (首输入) lifecycle.establish_baseline()  # SnapshotLifecycle: L2Sqlite 位拷贝
      │  │  └─ lifecycle.begin_run()               # Reset: reset_state(); Snapshot: no-op
      │  ├─ dispatch(data)                         # entry_driver.py:132
      │  │  └─ dispatch_seed → decode_sequence_seed # {"steps":[...]}? → execute_sequence
      │  │       └─ target(json.dumps(step))       # 逐步;legacy 字节 → target(data) 原样
      │  └─ (target 异常吞掉,不中断引擎)            # candidates.py:73
      ├─ with session.untraced():                  # tracer.py:96 —— 挂起 settrace
      │  └─ result = execution.finish_run(data)    # execution_context.py:283
      │     ├─ observer.observe()                  # 还原前!读的是输入执行终态;失败仅计数
      │     └─ lifecycle.end_run()                 # Snapshot: 关连接→gc→文件还原→内存钩子
      ├─ state_ledger.record(result.observations)  # ★ 返回的 novel 列表被丢弃(断点 1)
      ├─ mapped = map_trace(events[start:], ir)    # runtime/mapper.py
      ├─ obs = feedback.observe(mapped)            # structural.py:66 / frontier_feedback.py:62
      ├─ obs.interesting? → corpus.save(data,...)  # 唯一种子保留裁决
      ├─ feedback.update_global_coverage / commit
      └─ recorder.tick(exec_s, feedback_s, ...)    # 快照/候选 JSONL,逐行 flush 抗 _Exit
```

**观测时点**：`finish_run` 在整个输入（可能含多步 C10 序列）执行完之后、基线还原之前调用一次——即观测到的是**输入终态**，不是逐请求状态。`untraced()` 窗口保证观测/还原帧不进覆盖计数（`test_observer_frames_do_not_enter_coverage` 钉死）。

**vulnshop 靶侧双执行面**（`targets/vulnshop/fuzz_target.py:213` `target()`）：
- 请求 dict / `{"steps":[...]}` → `_request()`：Flask `test_request_context + full_dispatch_request` 真实全栈；`?token`/`?oid` 符号槽经模块级 `_BINDINGS` 表运行期代入，`extract` 回填（`:173-199`）。
- 非 JSON 字节 → `_legacy()`：`FlaskAdapter` 路由枚举，**无认证无请求体**——深状态对盲字节关闭（E vs D1 对照的前提）。

### 1.2 epoch 循环与轮间注入（E 臂）

```text
run_epochs (epoch.py:297)
└─ for epoch in 1..N:
   ├─ policy="state" and epoch>1:
   │  └─ _inject_state_seeds_between_epochs (epoch.py:143)
   │     ├─ state = global_state.json                 # 上轮末覆盖 + (可选)state_evidence
   │     ├─ verified = _verified_state_atoms(state)   # P0-B 键;实验协议下恒缺 → 空
   │     ├─ now = reached_state_atoms(ir, target, state)   # ★ 覆盖代理(见 §3)
   │     ├─ state_ledger.json 差分 → new_atoms = now - prior
   │     ├─ new_atoms 非空 → SKIP_R2_PROGRESS(有进展让路)或 INJECT_ALWAYS
   │     └─ inject_state_seeds (state_seed_injector.py:639)
   │        ├─ appmodel_path(target) 可用?            # R7 双路径分派
   │        │  └─ _inject_appmodel: 未达原子(字典序) → render_seed
   │        │     (extract_flask→_complete_auth_chain→build_sstg→plan_backward)
   │        │     → encode_sequence_seed → spool+corpus 双写 → 审计 INJECTED/DUPLICATE/NO_PLAN
   │        └─ 否则微路径: synthesize_micro_model(op分派+M2候选join) → _plan_requests
   │           → plan_backward → BindingContext.render_step → C10 种子
   ├─ (公共)语料结转 corp_in_e{N} = spool∪lf_corpus   # R6: 与 policy 无关
   ├─ _run_driver(...)                                # epoch.py:28 —— 子进程 entry_driver
   │    ★ 不传 --lifecycle/--observe-state(断点 3)
   ├─ _load_epoch_summary → rec["wall"]/["state"]
   └─ 结束: group_summary.json(state_atoms_reached/verified/…)
```

**规划触发（R2）依赖覆盖代理，不依赖 verified**：`_inject_state_seeds_between_epochs` 里 `prior`/`now` 均来自 `reached_state_atoms`（proxy 口径）；`verified` 只写进 `state_ledger.json` 的 `verified_reached_atoms` 键作记录（`epoch.py:191-196`），从不参与 `new_atoms` 差分。docstring 自认"两口径的对齐留给阶段 2 统一调度器"（`state_seed_injector.py:53-55`）。

### 1.3 F 臂 / 离线 goal 闭环（有执行验证的那条线）

```text
F 臂: epoch 轮间 → inject_goal_seeds (goal_seed_injector.py:37)
  未覆盖深块(global_state.blocks − covered,_handle 内含调用语句)
  → plan_for_block (goal_to_plan.py:331)
     derive_state_goal(必要守卫边→CONTROL 谓词,方向感知反转)
     → synthesize_appmodel(op 臂→Endpoint) → build_sstg → plan_backward
     → final-action 补尾 → 参数绑定 → identity-refresh(NE 谓词)
  → GoalCheckpoint → inject_checkpoint_seed (goal_checkpoint.py:89)
  ★ 注入前不执行——种子交给下一轮 libFuzzer,判达仍是覆盖代理

离线(单测/goal_directed_eval): execute_plan_for_block (plan_executor.py:132)
  plan_for_block → execute_plan(逐步 target + TraceSession + 写块覆盖验证)
  失败 → 整链重推导重试(max_replan)
  判达 → checkpoint_from_execution (goal_checkpoint.py:68)
       → inject_checkpoint_seed(C10 回注)
       → replay_checkpoint 复用重放(判据 = 目标块仍覆盖)
```

### 1.4 物理快照线（与 GoalCheckpoint 无关）

`runtime/snapshot/`：`supervisor.py`（跨进程,ANOTA 解释器内 fork-COW 服务）→ `engine.control_loop`（serve/freeze）→ `l2_sqlite.L2Sqlite`（文件 DB 位级模板）。`SnapshotLifecycle`（`execution_context.py:133`）**只复用 L2Sqlite 原语**，不包引擎（跨进程 epoch 服务与进程内 `target(data)` 生命周期不匹配——docstring `:18-22` 的裁决）。`SnapshotReplay`（`snapshot_replay.py`）是 PlanSegment ↔ checkpoint 叶对齐的执行器，走 `CheckpointHandle.serve` HTTP。

---

## 2. 三件状态容器：职责与重复性（审计问题 1）

| 容器 | 文件 | 保存什么 | 去重键 | 接入状态 |
|---|---|---|---|---|
| `StateRegistry` | `state/observer.py:43` | M1 动态**抽象状态节点**（`AbstractState.canonical_key` = 全部 session 变量抽象值拼接 `session.k=EQ(admin)\|...`）+ M2 迁移边容器 | 节点: canonical_key；边: `(from_key,to_key,trigger)` | **仅单测**（test_state_m1/m2）与 `cli state` 静态产物的 note 文本 |
| `TransitionRecorder` | `state/transition.py:55` | 同一逻辑会话内相邻两次 session 观察的状态差 → `TransitionEdge`（evidence 固定 `["dynamic:state-diff"]`） | 同上（经 add_edge） | **仅单测**；`state_seed_injector.py:47-50` 明确声明"本模块不实例化 TransitionRecorder" |
| `StateEvidenceLedger` | `state/runtime_observer.py:170` | P0-B 跨输入 **RUNTIME_VERIFIED 事实**（`(StateKey.render(), value)` 首现即新颖）+ 独立 proxy 原子桶 | `(render, value)` / render | **已接线** entry_driver（仅 `--observe-state`），跨 epoch 结转（C8 演进键 `state_evidence`） |

**结论：不重复，但分属两代语义且词表不通**。M1/M2 是"session-dict 全变量抽象快照"路线（Phase 4.3/5A 时代）；P0-B 是"谓词级三值证据"路线（阶段 1）。M1 的 `AbstractState` 把每个变量的抽象值拼成一个全局键——一个变量变化即新键，粒度粗、无实体/作用域维度；P0-B `StateKey` 有 `(predicate, entity, scope)` 但谓词词表由靶侧观测器自报，与 SSTG 原子无映射。**没有任何代码在这两套（加上 SSTG 原子共三套）词表间互译。**

---

## 3. 状态的可观测性分层（审计问题 3）

| 层 | 机制 | 词表 | 位置 |
|---|---|---|---|
| RUNTIME_VERIFIED | 靶侧观测器只读直查应用真源 | `StateKey`，如 `Order.status=PAID{3@resource}` | `targets/vulnshop/state_evidence.py:70`（users/orders/coupons 三表五谓词，双极）；`vulnshop` 的 `Session.*` 显式不可观测（Bearer 无状态 token） |
| CONTROL_FLOW_PROXY | ① 微路径：守卫真分支行集 ∩ covered_blocks 行集；② AppModel 路径：**写点**行覆盖（值匹配锚点优先，值盲锚点兜底；authed 原子锚 login 成功 return 行） | SSTG requires 原子，如 `Session.role=admin`、`Order.status=PAID` | `state_seed_injector.py:498` / `vulnshop_state_bridge.py:325` |
| STATIC_INFERRED | SSTG 的 requires/produces 边、M2 静态候选、StateFactCandidate | `Var=value` / `function/var/op` | `analysis/builder.py:45`、`state/transition.py:121`、`state/extractor.py:406` |
| UNKNOWN | 无观测器 / 未开 `--observe-state` / `Session.*`（vulnshop） | 台账缺位表达（"未观测"≠"观测为零"，`epoch.py:282-294` 双 None 语义） | — |

**已知过近似（自认的诚实边界）**：
- 值盲锚点对多值变量可能**超前判达**（`vulnshop_state_bridge.py:36-39`，known-debt #5）；
- "末 return=成功路径"启发式在多 return 非末成功形态会错锚（known-debt #4）；
- 微路径"守卫真分支已覆盖 ⇒ 原子判达"没有验证谓词取值方向（真分支覆盖蕴含守卫为真，这点成立；但产不出"谁生产的"）。

---

## 4. 种子保存的真实条件（审计问题 5、6）

`CandidateCollector.handle`（`candidates.py:92-97`）：

```python
if self.corpus is not None and obs.interesting:      # interesting = N_B+N_E > 0
    cid = self.corpus.save(data, ...)
```

`FrontierCollector.handle`（`frontier_collector.py:80-90`）：`interesting = 新BB ∨ 新边 ∨ 组合冠军挑战保留`（`frontier_feedback.py:76-84`，reason ∈ new_blocks/new_edges/frontier_champion/frontier_progress）。

两者都在保存**之前**调用 `state_ledger.record(result.observations)`，但返回的新颖事实列表被丢弃——**状态证据是纯旁路记账**。因此：

- 新 verified 状态**不能**触发种子保留；
- 新 verified 状态**不能**调整任何预算（STATE_BUDGET=3 常量，`state_seed_injector.py:86`）；
- 语料条目 reason 词表（`corpus_manager.py:29` interesting_reason）没有 state_* 取值。

这是 Phase 2 最小改动点：`obs.interesting` 裁决处加入可选状态新颖度信号（开关控制）。

---

## 5. 逐请求迁移的现状（Phase 2 前置缺口）

- 输入级观测：`ExecutionContext.finish_run` 每输入一次，读输入终态（`execution_context.py:283-306`）。
- 请求级迁移记录：`TransitionRecorder.observe(request_id, session)` 已实现（含会话边界 `new_session`），但**没有任何生产路径调用它**。`grep TransitionRecorder` 全仓只有：定义、observer.py 注释、cli.py 注释、state_seed_injector docstring（声明不用）、两个单测。
- C10 序列在 `execute_sequence`（`entry_driver.py:60-70`）里逐步执行，**步与步之间无观测钩子**；序列内一个请求写、下个请求读的状态依赖只能靠覆盖代理事后推断。
- `StateObservation` 是谓词级快照，不是 before/after 对——Phase 2 需要在请求边界成对采样（或复用 TransitionRecorder 的 diff 思路）并处理"中间瞬态"（输入终态可见 ≠ 序列中间可见）。

---

## 6. GoalCheckpoint 与物理快照的边界（审计问题 9、10）

**GoalCheckpoint（`solving/goal_checkpoint.py`）= 逻辑请求前缀**：
- 数据 = `steps: list[dict]`（C10 步）+ 目标块 + 约束 + 证据（`checkpoint_from_execution:68` 要求 `execution.target_covered and execution.steps` 才存档）；
- 复用 = `replay_checkpoint:106` 用 `execute_plan` **重新执行整链**（零推导），判据 = 目标块仍覆盖；
- 注入 = `inject_checkpoint_seed:89` spool+corpus 双写（内容哈希去重，origin="GOAL"）。

**SnapshotLifecycle / runtime/snapshot = 物理状态恢复**：
- `SnapshotLifecycle.establish_baseline`：L2Sqlite `snapshot_template`（live→tpl 位拷贝 + `-journal/-wal/-shm` unlink，`l2_sqlite.py:32-42`）；
- `end_run` 还原顺序（固定）：pre_restore_hooks（关连接）→ `gc.collect()` → L2Sqlite.restore → memory_hooks（`execution_context.py:186-202`）；模板丢失 = LifecycleError（fail-closed，`l2_sqlite.template_exists` 收口）；
- **实际恢复范围**：声明于 `SF_STATE_FILES` 的文件（vulnshop=进程私有 DB）+ `SF_MEMORY_RESET`（vulnshop=清 `_BINDINGS`）。**不恢复**：进程内其余内存态（微靶 `_SESSION/_DB` dict 走 ResetLifecycle 另一路径）、cookie jar（vulnshop 无 cookie 面）、Flask app 级缓存、观测器自身状态。
- **已知一致性风险（文档自认）**：WAL 模式下未 checkpoint 的页在 `-wal` 里，基线捕获必须在静默点；当前靶（journal_mode=DELETE）无此问题，但通用性未保障（`execution_context.py:146-149`）。restore 前强关连接+gc 的顺序防线已建；但"连接持有的页缓存 vs 还原后文件"的漂移在非 SQLite 句柄（如 mmap 文件）上无泛化验证。

---

## 7. 通用层里的靶场专属规则（审计问题 11）

| 位置 | 内容 | 性质 |
|---|---|---|
| `state/planning/compat.py:21-28` | `JSON_FILL`：`coupon_code=SAVE10`、`items=@items`、`role=admin` 等 vulnshop JSON 槽位预填 | 靶场专属（自认兼容层，消融臂按"无本层"对照） |
| `state/planning/compat.py:42-52` | `ensure_auth`：`/auth/login`、`/auth/register` **路由后缀特判** | vulnshop 路由假设 |
| `state/planning/goals.py:42-101` | `VULNSHOP_GOALS` 5 个 goal 模板（goal 名、路由、JSON 键、`Coupon.used=1`、`SAVE10`） | 靶场专属（goal 库本身是模板集合，属数据而非算法） |
| `analysis/builder.py:93` | `if var == "Coupon.used" or inv.one_way:` —— **SSTG 构建器硬编码靶场变量名** | 靶场专属泄漏进 L1（应泛化为模型声明的 one_way 属性） |
| `solving/vulnshop_state_bridge.py:75-76` | `_PUBLIC_ROUTES=("/auth/register","/auth/login","/")`、`/auth/login` 后缀、`Coupon.used` 单向 | 自认"模块名沿用 vulnshop 例名"，逻辑对任何 Flask 靶开放但路由假设是 vulnshop 形状 |
| `solving/vulnshop_state_bridge.py:95-107` | `_success_return_line` 末 return=成功启发式 | vulnshop 早退惯用法内恒正确（known-debt #4） |
| `runtime/snapshot/supervisor.py:39-51` | `build_app` 按 `--target` 名直引 `targets.vulnshop/vampi` | 已登记 ARCHITECTURE.md 例外表第 3 行 |
| `state/planning/backward.py:176-180` | `route.endswith("/login")` → extract token；集合级 POST + observes 含 "id" → extract id | HTTP 惯用法（半通用），路由词仍是 vulnshop 形状 |
| `solving/state_seed_injector.py` | `op == "..."` 分派、`session`/`body` 变量名结构匹配 | 结构性微靶词汇（零预设字段名，但词汇面=op 分派靶） |

**结论**：专属规则大多已被自觉隔离（compat 层 / 桥 / goals 库 / 例外表），真正越界的硬编码只有 `builder.py:93`（L1 层认得 `Coupon.used`）和 supervisor 的靶名分派（已登记）。Phase 1 泛化时优先修 builder。

---

## 8. 旧 A–F 实验协议的约束面（审计问题 12）

- **组定义**：`runner.py:21` `GROUPS = ("A_native","A_measure","B","C","D1","D2R","D2P","E","F")`；campaign 聚合表/curves.csv 按固定组名循环（`campaign.py:261`）；`--groups` 子集可跑但组名不可扩。
- **驱动旗标**：`epoch._run_driver` 只传 `--seconds/--mode/--seed/--epoch/--group`。`--lifecycle`（默认 noop）与 `--observe-state`（默认关）**从未被实验协议设置**——新状态功能进实验必须走 `run_epochs` 的接缝参数（`state_trigger`/`state_injector` 模式）或新增 H 系列 CLI，不得改变 E/F 缺省行为。
- **公平性机制**：公共语料结转（R6：feedback 开 = spool∪corpus，否则仅 corpus，与 policy 无关，`epoch.py:359-370`）；每 epoch 真实墙钟起止（P0-4）；exec/feedback 分项计时；NovelSeedGain（P0-9）；manifest 哈希（target/ir）。
- **数据契约**（PROTOCOLS.md，测试钉死）：C1 ProgramIR（冻结）、C8 global_state.json（演进，`state_evidence` 键集冻结于 v1）、C9 snapshots 8 键（演进）、C10 steps 语义（演进，分派只看顶层 `"steps"` 键，legacy 逐字节不变）。任何 Phase 1–3 改动落在这些产物上都必须"只加不改 + `.get` 缺省读 + 同步 test_protocols.py"。
- **消融既有面**：`state_ablation.py`（E/E-rand/E-always，同动作集同预算同触发语义）；`four_arm.py`（B/C/D/E/F + F−E 配对差）。H 系列应以同形方式挂在 runner/campaign 侧，而不是改 E/F 语义。
- **结果文件**：`group_summary.json`（`state_atoms_reached`/`state_atoms`/`state_atoms_verified(_list)`/`state_injections_total` 已是演进键）、`epochs/state_ledger.json`（`atoms`=代理口径旧键保留 + `proxy_reached_atoms`/`verified_reached_atoms` 双口径）、`epochs/state_injections.jsonl`、`epochs/llm_injections.jsonl`、`lifecycle_failures.jsonl`。

---

## 9. 缺口清单（断点定位，按闭环序）

| # | 断点 | 证据位置 | 影响 |
|---|---|---|---|
| G1 | 状态新颖度不进种子保留 | `candidates.py:87`、`frontier_collector.py:74`（record 返回值丢弃） | 状态反馈不影响 Fuzzer 决策——研究主线 2 的前置 |
| G2 | 无逐请求状态迁移记录 | `execution_context.py:283`（输入级一次）；`entry_driver.execute_sequence:60`（无步间钩子）；TransitionRecorder 未接线 | 无法产出 Phase 2 的 verified 迁移、无法发现序列内依赖 |
| G3 | verified ↔ proxy 词表无映射 | `state_evidence.py:6-8`（"两组词汇互不冒充…阶段 2 调度器再负责对齐"）；`epoch.py:191`（R2 用 proxy） | 判达与证据永不对账；H1 臂指标（verified 状态覆盖）无法与 SSTG 目标对齐 |
| G4 | 无状态前沿对象 | `_appmodel_goals`（`state_seed_injector.py:584`）= 全原子−已达，**字典序**；无前置满足度/成本/尝试历史/停滞概念 | Phase 3 的核心对象不存在；"前沿"目前只是 frontier_feedback 的**代码边**前沿（词同名异） |
| G5 | 规划失败无记忆 | `_inject_appmodel:612-635`：失败链无账（无 NO_PLAN 之外的失败原因）；同原子下轮重规划同链（内容哈希 DUPLICATE 挡写但不记原因）；`PLAN_ERROR_*` 只记异常 | 无限重复规划风险（预算被 DUPLICATE 空转或反复换 goal 无解释） |
| G6 | 成功链不回注 fuzz 语料 | E 臂注入的种子判达后无回注动作；checkpoint 回注只在离线 goal_directed_eval/单测路径 | "规划结果反哺语料"闭环只在离线存在 |
| G7 | 实验协议不启用 P0-A/P0-B | `epoch.py:28-39` `_run_driver` 旗标硬缺 | verified 指标在 A–F 下恒为 None；H1 无法直接落地 |
| G8 | M1/M2 与 P0-B 双状态观测体系并存 | §2 | 语义重复但不等价；需在 STATE_CONTRACT 里统一或显式分工 |
| G9 | 三套词汇的实体/作用域缺失 | SSTG 原子无实体维度；vulnshop 观测器 entity=oid/username | `Order.status=PAID` 的 SSTG 原子无法区分订单 3 与订单 4（审计问题 2 的核心差异） |
| G10 | 观测失败/生命周期失败审计已有但 epoch 级不汇总 | `lifecycle_failures.jsonl` 写入（`execution_context.py:368`）；collector 计数（`candidates.py:51`）；group_summary 无汇总键 | 公平性记账缺口（观测开销与失败率不进结果文件） |

另：观测/规划/快照开销目前只有 exec_seconds/feedback_seconds 二分（feedback_s 包含 map+observe+save，`candidates.py:88-99`）——细分观测成本需在 recorder 层演进（C9 演进键纪律内）。

---

## 10. 已实现能力与证据（正面清单）

| 能力 | 位置 | 测试 |
|---|---|---|
| 输入级状态隔离（noop/reset/snapshot，fail-closed，poison 语义） | `runtime/execution_context.py` | `tests/runtime/test_execution_context.py`、`tests/fuzzing/test_lifecycle_wiring.py`（5 测试：默认逐字节不变/失败审计跳过/证据入账/观测帧不入覆盖/frontier 同形） |
| 三值证据契约 + 台账（verified/proxy 分桶、跨 epoch 结转、坏行跳过） | `state/runtime_observer.py` | `tests/state/test_runtime_observer.py`（8 测试） |
| 观测帧不入覆盖（untraced） | `runtime/tracer.py:96` | `test_observer_frames_do_not_enter_coverage` |
| C10 序列种子：编码/解码/分派/会话连续性 | `entry_driver.py:36-79` | `tests/fuzzing/test_sequence_seeds.py`、`test_protocols.py` C10 钉 |
| 反向规划（成本择优、soft-producer 兜底、attack drop_requires） | `state/planning/backward.py` | `tests/planner/`、`tests/state/test_goal_to_plan.py` |
| R7 双路径注入（AppModel 真路径 / 微靶合成路径） | `state_seed_injector.py` + `vulnshop_state_bridge.py` | `tests/solving/test_state_injector_vulnshop.py`、`test_state_seed_injector.py` |
| 目标块→约束推导（方向感知否定谓词、身份刷新步） | `goal_derivation.py`、`goal_to_plan.py:278` | `tests/state/test_goal_derivation.py`、`test_negated_predicates.py` |
| 计划执行+写块验证+重规划 | `runtime/plan_executor.py` | `tests/state/test_plan_executor.py` |
| 检查点存档/注入/复用重放 | `solving/goal_checkpoint.py` | `tests/solving/test_goal_checkpoint.py` |
| 停滞判停（StallTracker）/LLM 注入臂 | `solving/stall_tracker.py`、`epoch.py:73` | `tests/feasibility/` |
| 九组实验 + 消融 + manifest + AUC | `experiment/runner.py`、`campaign.py`、`state_ablation.py` | `tests/evaluation/` |
| 物理快照原语（fork-COW 引擎、L2 文件模板） | `runtime/snapshot/` | `tests/snapshot/`、`tests/integration/` |
| 分层与协议机检 | ARCHITECTURE.md + PROTOCOLS.md | `test_architecture_boundaries.py`、`test_protocols.py` |

---

## 11. 风险表（Phase 1 输入）

| 风险 | 等级 | 说明 |
|---|---|---|
| WAL 快照一致性 | 中 | 文档自认静默点假设；泛化靶（WAL 模式）下 SnapshotLifecycle 基线可能丢未 checkpoint 页。Phase 1 任务 1 的第一项。 |
| `untraced()` 重入/多线程 | 低 | 保存 `sys.gettrace()` 恢复；单线程假设下成立，atheris worker 线程外无观测并发。改多步观测时保持窗口纪律。 |
| 覆盖代理超前判达 | 中 | 值盲锚点（known-debt #5）会让 R2 提前 SKIP；H1/H3 若不换 verified 口径会自肥。 |
| 微靶词汇面窄 | 中 | op 分派 + `session[...]` 惯用法；非该形状靶（Flask 真 AppModel 路径之外）诚实回落（synthesize_micro_model → None）。 |
| epoch 子进程超时启发式 | 低 | `timeout=seconds*4+120`（`epoch.py:36`）；观测开销增大后余量是否够需在 H 系列校准。 |
| 三套词表统一时的兼容破坏 | 高 | C8 `state_evidence` 键集冻结于 v1、C9 8 键、C10 语义——统一契约必须以演进键方式叠加，测试钉住旧行为。 |
| lru_cache 共享模型可变 | 低 | `synthesize_micro_model`/`appmodel_path` memo（P4-T2）声明只读共享；未来若有人原地改 SSTG 即破——Phase 3 前沿对象若挂在缓存模型上需只读纪律或显式克隆。 |

---

## 12. 审计问题速查表（对照任务书 12 问）

1. **三容器**：见 §2——不重复、两代并存、词表不通。
2. **SSTG 原子 vs 运行时键**：SSTG `"Var=value"`（无实体/作用域；`<authed>`/`*` 特值）vs `StateKey(predicate,entity,scope)`（实体=oid/username，作用域五值）；另有 M1 全变量联合键。见 §2、§9-G9。
3. **可验证/代理/不可观测**：§3。
4. **调用链**：§1.1–1.3（单请求逐步列；多请求 = execute_sequence 逐步 + 输入级一次观测）。
5. **种子保存条件**：§4（结构新颖度 or 前沿冠军；状态零参与）。
6. **新状态触发保留/预算**：不能（G1）。
7. **规划触发口径**：覆盖代理（`epoch.py:191`），verified 仅记录（G3）。
8. **规划验证/失败/重复**：E/F 臂无执行验证（代理判达）；失败仅 NO_PLAN/PLAN_ERROR 审计、无失败原因记忆、无计划签名（G5）；重复注入由内容哈希 DUPLICATE 挡住；离线路径有执行验证+checkpoint 回注（§1.3）。
9. **GoalCheckpoint**：逻辑请求前缀（§6）。
10. **SnapshotLifecycle 恢复面**：声明文件 + 内存钩子；不覆盖连接/cookie/其余内存；WAL 风险（§6）。
11. **靶场专属规则**：§7（builder.py:93 为唯一越界硬编码）。
12. **A–F 约束**：§8。

---

*本文为 Phase 0 交付物之一；配套契约草案见 `STATE_CONTRACT.md`，整合方案见 `INTEGRATION_PLAN.md`。Phase 0 不修改任何核心算法。*
