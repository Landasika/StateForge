# Session-First 基本块状态依赖与定向 Fuzzing 计划

日期：2026-10-19
基线提交：`9f7a559e637d5b549f379bfa6a1d50c71dd13fd8`
范围：仅 SESSION 域；不实现 DATABASE / FILE 状态依赖，不调用 LLM，不重写 SSTG。

> 本文是任务书与实施记录，不替换 `STATE_CONTRACT.md`。后续批次按
> SS-00 → SS-01 → SS-02 → SS-03 → SS-04 顺序推进，每批独立提交、独立验收；
> 未通过当前批次不进入下一批。

---

## 1. 架构共识（必须遵守）

1. **统一锚点是 BasicBlock。** 保留 `ProgramIR.blocks`、ICFG、Trace/Mapper 的
   `BlockID` 语义；Session 读写只是附着在代码位置上的访问事件，不另建业务
   状态节点图。
2. **静态可能影响 ≠ 运行事实。** `session['uid'] = ...` 只是 STATIC_MAY；
   经过写入块只是覆盖证据；只有后续请求通过真实会话机制读回相应值，才可
   说会话条件已验证。`RUNTIME_VERIFIED` 与 `CONTROL_FLOW_PROXY` 不得混桶。
3. **SESSION 是「客户端/会话上下文 + key + value」。** 同 key 不同客户端
   不能合并；动态未知 key 保留不确定性；`clear()` 影响所有 key。存储后端
   与客户端作用域不能压缩成一个 `session.uid` 字符串。
4. **Session-first 不等于 Bearer/JWT。** Flask 默认签名 Cookie Session 的
   写请求会经响应 `Set-Cookie` 更新客户端 Cookie Jar；无状态 JWT 的响应
   令牌传递属于请求间数据依赖，本轮不伪装为服务端 Session 写入。
5. **一个 fuzz 输入可含多请求。** 输入内共享 Cookie Jar / 客户端上下文；
   输入之间隔离（新客户端/空 Cookie Jar，或后续受控基线恢复）。检查点先
   保存可重放的请求前缀，不急于通用物理快照。
6. **未知不判失败。** 未知 key、无法解析的封装调用、不可靠观测一律
   UNKNOWN / PARTIAL；不猜、不把未观测事实变成 FALSE。
7. **不预置答案。** 静态图只提供候选写入块；有效多请求序列由 Fuzzing 在
   同一客户端上下文中搜索。靶/语料可提供合法单请求，不得提供排好序的
   完整成功序列。

---

## 2. 批次与验收门

| 批次 | 主题 | 依赖 | 产出 / 验收门 |
|---|---|---|---|
| SS-00 | 测试靶与基线 | 无 | 可独立运行的 Flask Cookie-Session 靶 + 金标准基本块与成功/失败序列 |
| SS-01 | Session AST 读写提取 | SS-00 | 稳定访问事件 + 源码/BlockID 锚点；同一入口产出 |
| SS-02 | SessionAccessIndex 与目标依赖查询 | SS-01 | 从读取目标反查潜在写入块，保留值约束与别名/客户端条件 |
| SS-03 | 定向多请求搜索（不预置链） | SS-02 | 通过现有 C10/Atheris 搜索出有效 Session 序列 |
| SS-04 | 运行时 verified、失败反例与检查点复用 | SS-03 | 独立重放、客户端隔离、可信判达、复用效果报告 |

执行纪律：一次只做一个 PR；当前批次测试日志与产物必须可复现。若最新代码
已实现某个子项，做接线/测试/修复，不重复实现。

---

## 3. SS-00：最小 Flask Cookie-Session 靶

### 3.1 落地文件

| 文件 | 作用 |
|---|---|
| `stateforge/targets/session_gate/app.py` | 极小 Flask 靶：默认签名 Cookie Session，无 DB/文件/JWT |
| `stateforge/targets/session_gate/fuzz_target.py` | Atheris `target(data)` / C10 单请求适配，输入边界隔离 |
| `stateforge/tests/targets/session_gate/test_gate_semantics.py` | 只验证靶行为与适配层，不调用 Session 分析器 |
| `docs/architecture/SESSION_FIRST_PLAN.md` | 本计划与 SS-00 记录 |

### 3.2 路由与基本块

| 路由 | 行为 | 标记基本块 |
|---|---|---|
| `POST /authenticate` | `form.passcode == 配置口令` 时 `session['uid'] = 7`，否则 403 且不写会话 | — |
| `GET /protected` | `uid = session.get('uid')`；`uid is None` → 401，否则进入目标块 | `target_block` |
| `GET /protected_truthy` | `x = session.get('uid'); if x:` 守卫变体 | `truthy_target_block` |
| `GET /protected_membership` | `'uid' in session` 守卫变体 | `membership_target_block` |
| `GET /protected_subscript` | `session['uid']` 读取（KeyError → 401） | `subscript_target_block` |
| `POST /logout` | `session.pop('uid', None)` | — |
| `POST /clear` | `session.clear()` | — |
| `POST /noise` | `session['other'] = 7`（成功写其他 key） | — |

固定合法口令 `valid` 只在靶配置
（`DEFAULT_PASSCODE` / `SESSION_GATE_PASSCODE`）中给出，供单请求字典
使用；模块不导出任何有序多请求序列。目标块以函数标记，后续由 ProgramIR
给出真实 `BlockID`，不在分析器中硬编码名称或 ID。

### 3.3 客户端隔离与只读观测

- `reset_state()`：创建新的 Flask test client（空 Cookie Jar）并清空目标
  命中台账；即 `--lifecycle reset` 约定钩子。
- `input_scope()`：进入时 `reset_state()`；退出时只替换客户端，保留本次
  输入的目标命中台账供断言。以此保证「输入内共享、输入间隔离」。
- `target(data)`：接受单个 C10 请求 dict（`method`/`path` 与
  `query`/`form`/`json`/`header`），也接受直接测试用的整段
  `{"steps": [...]}`；非 JSON/非 dict 确定性 no-op。输入提供的 `cookie`
  被忽略，不能伪造 `session` Cookie 解锁目标。
- `observe_session(client)`：从客户端 Cookie Jar 读取当前签名会话 Cookie，
  用应用自己的 `SessionInterface` 只读解码；不建请求上下文、不触发路由、
  不修改 Cookie Jar。状态词表：
  - `absent`：该客户端没有会话 Cookie；
  - `decoded`：签名有效且 payload 为 mapping（`{}` 也是解码成功）；
  - `invalid`：Cookie 存在但签名/payload 无效；
  - `unknown`：无法观测（如无 serializer）。
- 业务路由只按口令写 `uid`；测试通过真实请求获得会话，不使用
  `session_transaction()` 直接注入成功状态。

### 3.4 SS-00 验收矩阵

| 场景 | 预期 |
|---|---|
| 匿名 `GET /protected` | 401；目标块 Trace 未覆盖 |
| 合法单请求写 → 同 client `GET /protected` | Cookie 解码 `uid=7`；进入目标块 |
| 非法口令 | 403；无会话 Cookie；后续 401、目标未覆盖 |
| 写 `other` | 200；`uid` 不存在；后续 401、目标未覆盖 |
| 写 `uid` 后 `logout` / `clear` | 会话撤销；后续 401、目标未覆盖 |
| 合法写后另一个空 client 读取 | 401；空 client 无 Cookie；原 client 仍可进目标 |
| 一次输入合法、一次空输入 | 后一输入不得借用前一 Cookie |
| HTTP 2xx 的 `/noise`、`/logout`、`/clear` | 均不得算作目标块覆盖 |
| 守卫变体 | 各自只覆盖自己的标记块，不冒充主目标块 |
| 输入注入伪造 session Cookie | 被忽略；目标未覆盖 |

测试命令（本仓库实际路径）：

```bash
python -m pytest stateforge/tests/targets/session_gate/ -q
```

### 3.5 SS-00 实施记录

- 基线 `9f7a559`；新增靶、适配器、语义测试与本文档。
- 未修改 `stateforge/state/extractor.py`、`stateforge/state/index.py`、
  `stateforge/fuzzing/entry_driver.py`、ProgramIR/BlockID、C10 序列格式。
- 测试结果：`14 passed`（2026-10-19，`python -m pytest
  stateforge/tests/targets/session_gate/ -q`）。
- 已知边界：本批只验证靶与输入边界；没有 Session AST 提取、没有索引、
  没有定向搜索/verified 检查点。变体守卫中的 `uid=0/False/''` 语义留待
  SS-04 的运行时验收矩阵，不在本批用测试注入伪造。

---

## 4. 后续批次要点（已由 §7 实施，保留原始验收口径）

### SS-01 Session AST 读写提取

- 扩展 `state/extractor.py` 的 `_extract_session` /
  `_session_alias_predicates`，同一提取入口产出 `SessionAccess`；不另造
  第二套全功能静态提取器。
- 支持：`session['uid']=7`（仅 SET）、`x=session['uid']` / `.get` /
  `'uid' in session`（READ/EXISTS_CHECK）、`pop`（READ+DELETE）、
  `del`、`clear()`（ANY）、`update({'uid':7})`、`+=`（READ_MODIFY_WRITE）、
  简单局部别名与 `from flask import session as sess`。
- `site_id` 区分同一行/同一块多访问；动态 key 保留 SYMBOLIC/UNKNOWN，
  不静默漏掉、不宣称 MUST；静态事件永不进 verified 桶。

### SS-02 SessionAccessIndex 与目标查询

- 优先扩展 `state/index.py`；索引键含 backend、key、client 约束，保留
  writers/readers/deleters；不生成所有 writer×reader 笛卡尔积。
- `SessionDependency` 的 `key_overlap` 为 MUST/MAY/DISJOINT；不同已知
  client 无值转移时 DISJOINT；静态不知 client 时携带同客户端约束。
- `query_session_frontier(ir, index, target_block)` 复用已有 ICFG/必要
  分支与局部数据流，保留多组路径约束；不能证明同一 client 时标 PARTIAL；
  `clear()` 是能破坏/满足「不存在」的候选，不能直接满足 `uid == 7`。

### SS-03 定向多请求搜索

- 在 `frontier_feedback.py` / `entry_driver.py` 增加独立开关
  （如 `--session-guided`），不改变 legacy 单请求与 A–F 默认行为。
- 维持 `encode_sequence_seed` / `decode_sequence_seed` 的
  `{"steps": [...]}` 格式；结构感知变异（替换/追加/删除/参数微变），
  在有效前缀上优先变异末步；固定 seed、`max_steps`、`max_execs`、序列
  哈希去重。
- 只给目标块和合法单请求池；不得注入完整 `login→profile` 顺序。
- 写块覆盖只算进展；目标到达来自 Trace → Mapper → `BB_target`；无可靠
  观测器时只能报 PROXY_PROGRESS，不能标 verified checkpoint。

### SS-04 真值、隔离与检查点

- 三层判定分离：`STATIC_MAY_DEPENDENCY` / `VERIFIED_SESSION_CONDITION` /
  `TARGET_REACHED`；只有后两者且隔离/复现成立才保存 verified 前缀。
- 观测时点：请求完成、Cookie Jar 保存后、下一请求前；只读；不污染
  Trace；异常/失败 → UNKNOWN，不自动升级。
- `SessionReplayCheckpoint` 仅收录 verified 前缀；冷启动新建干净客户端
  重放前缀再执行新后缀；每个后缀独立，不能复用前一后缀修改后的状态。
- 反例必须覆盖：未登录、异 client、写 other、pop/clear、失败写入、
  未知 key、`uid=0/False/''` 的 EQ/TRUTHY/EXISTS 差异。

---

## 5. 禁止事项

- 不实现 DATABASE/FILE 新依赖分析，不顺手修改其他领域契约。
- 不调用/集成 LLM 做源码语义、请求规划或 Fuzzing 策略。
- 不硬编码路由名、变量名、BlockID 或固定两步序列让测试过关。
- 不把 HTTP 200、写块覆盖、响应含 `Set-Cookie`、静态 MAY 边单独当作
  「目标状态已验证」。
- 不合并跨客户端/跨时刻事实；不把 UNKNOWN 当 False；不把 TRUTHY 当
  EXISTS。
- 不修改 C10 序列格式、ProgramIR BlockID、`StateKey.render()` 或历史 JSON
  协议。
- 不新建与 `StateEvidenceLedger`、`RequestLevelHarness`、
  `StateAccessIndex` 功能重复的平行系统。

## 6. 停机条件

出现跨客户端假成功、错误重放成功、覆盖冒充 verified、C10 语义破坏、
旧测试回归，或环境缺失导致关键验收未执行时，停止下一批并报告，不继续
堆代码。


---

## 7. SS-01–SS-04 实施记录（2026-10-19）

### 7.1 提交链

| 批次 | 提交 | 内容 |
|---|---|---|
| SS-00 | `af3c50a` | Flask Cookie-Session 靶、C10/单请求适配、输入级隔离、14 测试 |
| SS-01 | `a183251` | `SessionAccess` 访问事件、BlockID/源码锚点、别名/动态 key、12 测试 |
| SS-02 | `4b776ed` | `SessionAccessIndex`、`query_session_frontier`、路径约束分组、13 测试 |
| SS-03 | `0752a3c` | 有界定向多请求种子搜索、独立 `--session-guided`、C10 注入、9 测试 |
| SS-04 | `1d4a00f` | 运行时 verified Session 观测、条件判定、solving 层检查点/冷启动重放/隔离、26 测试 |

### 7.2 SS-01 实际落地

- 新入口 `state/session_access.py::extract_session_accesses` 与
  `extract_session_facts`；旧 `analyze_session_access` 兼容保留，M1 旧测试
  无回归。
- 支持 SET/READ/EXISTS/GET/POP/DELETE/CLEAR/RMW/UPDATE/SETDEFAULT、
  常量/SYMBOLIC/ANY/UNKNOWN key、`from flask import session as sess`、
  `import flask`/别名、局部遮蔽、嵌套函数不双计。
- `site_id` 稳定唯一；有 IR 时绑定真实 `BlockID`；所有静态事件
  `STATIC_MAY`。
- 测试：新增 12；与旧 M1/提取合计 `37 passed`。

### 7.3 SS-02 实际落地

- `state/session_index.py`：按 key 桶的 index、逐 reader 的
  `writers_of`、`SessionDependency`、`SessionFrontier`。
- `query_session_frontier` 复用 `goal_derivation` 的函数内投影，逐路径
  保留条件组；歧义别名降级 `KEY_UNKNOWN`；非 Session 守卫 unresolved；
  `clear()` 为 ANY；不同常量 key 为 DISJOINT；同客户端约束始终携带。
- 测试：`14 passed`（索引 11 + goal-query 契约 3）。

### 7.4 SS-03 实际落地

- `fuzzing/session_guided.py`：有界结构搜索（append/replace/delete/
  parameter mutation）、规范化哈希去重、固定 seed/预算、每次候选 reset。
- `entry_driver.py` 新增 `--session-guided` 等独立开关，默认关闭；
  启动前把有效 C10 序列写入语料，Atheris 仍是唯一变异/执行引擎。
- 无 probe 只报 `PROXY_PROGRESS`；有 probe 且条件组满足才 `VERIFIED`。
- 测试：`9 passed`；端到端冒烟发现 `POST /authenticate(valid)` →
  `GET /protected`，搜索 15 次执行。

### 7.5 SS-04 实际落地

- `state/session_observation.py`：只读 Cookie Jar 观测、三值判定
  （VERIFIED_TRUE/VERIFIED_FALSE/UNKNOWN）、`StateObservation` 绑定
  client/observation point、NULL/MISSING/UNKNOWN 分离。
- `solving/session_replay.py`：`SessionReplayCheckpoint` 仅由
  `VERIFIED` 结果创建；冷启动前缀重放、重新判定条件、后缀独立执行、
  后缀单独执行隔离对照、C10 种子注入去重。
- `fuzz_target.py` 暴露 `session_view()` / `sf_state_observer()`，接入
  既有 `RuntimeObserver` 与 `StateEvidenceLedger`。
- 测试：新增 26；与 SS-00 靶合计 `40 passed`。

### 7.6 实验结论（详见 `docs/reports/SESSION_FIRST_EXPERIMENT.md`）

- 同一请求池/预算/隔离，3 seeds：
  - BASE：目标 0/3，writer 3/3，verified Session 3/3（单请求登录写状态，
    但没有组合出受保护请求）；
  - SESSION-GUIDED：目标 3/3，检查点 `sc-49405742647b`；
  - SESSION-REUSE：目标 3/3。
- 检查点冷启动 replay：`REPLAY_VERIFIED`，`uid=7`；后缀无前缀隔离
  对照通过。
- 静态查询：9 个访问全 `STATIC_MAY`，0 UNKNOWN；候选写块
  `authenticate`/`logout`/`clear`。
- 这是 3 seeds、2 秒、单微型靶的功能性证据，不宣称统计显著或通用
  性能优势。

### 7.7 最终回归

- 全量：`731 passed, 1 skipped, 26 deselected, 1 warning in 660.46s`
  （相对基线 656 增加 75 个测试；SS-00 14 + SS-01 12 + SS-02 14 +
  SS-03 9 + SS-04 26）。
- 架构边界：`5 passed`（`session_replay` 放在 solving 层，避免
  state → runtime/fuzzing 向上引用）。
- micro_benchmark：`exit=0`，静态 21 用例与动态 5 用例全 1.00。

### 7.8 已知边界

- 仅 Flask 默认签名 Cookie Session；其他 Session 后端需新 observer。
- `absent` 只在该 client/observation point 内权威；不跨 client 合并。
- 静态查询可 `partial`；不把未证明的路径/客户端约束升级为事实。
- `SessionReplayCheckpoint` 是 Session 域包装，复用 C10、
  `StateEvidenceLedger`、reset lifecycle，不复制 ledger。
