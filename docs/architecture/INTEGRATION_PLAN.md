# StateForge 整合计划（v1.1，修订版）

> Phase 0 契约修订轮交付物。按《Phase 0 契约修订任务》修订 v0：T1（R7 SQLite/WAL 快照一致性）重设计，T2–T7 与 `STATE_CONTRACT.md` v1.1（R1–R6）同步，验收门更新，并给出 **Phase 1 精确修改清单**（§5）。
> 组件处置表（v0 §1）延续不变：全部保留/适配/扩展，零废弃、零重叠新建；`StateRegistry`/`TransitionRecorder`/`StateEvidenceLedger` 职责边界保留；A–F 缺省行为不变；新能力走独立开关。本轮**未修改任何代码**。

---

## 1. Phase 1 任务（T1–T7，修订版）

> **执行批次（v1.1c / PR-00 裁决,消除 v1.1b 的循环依赖)**:
> v1.1b 曾把"完整 M0"既设为第二批(T5b)开工前提、又依赖 T5b 产出——循环。
> 现拆 M0 为三段并重排依赖(详见 §1-M0 拆分与 §8 依赖图):
> **M0-A 静态关系提取**(已具备原型,25 专项测试) → **PR-01 CFG 正确性**(已完成) →
> **PR-02 可靠输入级快照(=T1) → PR-03 结构化事实+联合求值(=T2/T3/T4 核心) →
> PR-04 M0-B 已知链逐请求联合验证 → PR-05 M0-C 自动规划+冷启动重放 →
> PR-06 失败约束/去重/修复 → PR-07 fuzz 接入与无 LLM 消融**;
> T5a(映射适配器)/T5b(关系提取原型)不重做;T6/T7 并入 PR-07 前置。
> 顺序即依赖序。每项含改动点、验证命令、对应契约条款。与 v0 的差异以〔修订〕标注。

### M0 拆分与目标钉死（PR-00 裁决）

- **M0-A 静态关系提取**：`derive_relational_goal` 原型 + 25 项专项测试（`test_relational*.py`）——已完成，产物样例 `artifacts/m0/relational_goal.json`。
- **M0-B 已知请求链联合验证**：五步已知链（register→login→create→pay→ship）逐步 before/after 观测 + 单点联合验证 + Trace 独立确认 UPDATE 块——**不要求自动规划**；依赖 PR-02 + PR-03。
- **M0-C 自动规划与独立重放**：完整关系合取进规划器、冷启动自动到达、资源 ID 经 extract 重建的独立重放——依赖 M0-B 通过。
- **目标钉死**：GUARDS=1；目标基本块 = `app.py` `ship_order` 内 `UPDATE orders SET status='SHIPPED'`；块 ID 由 `build_icfg` 生成（`app.py::ship_order::275-275::block::1`，行号随靶版本漂移——测试一律按 statements 含 `UPDATE orders SET status='SHIPPED'` 且 `in_file="app.py"` 定位，断言行号锚点同步维护）。
- **契约未冻结问题登记（各自归属 PR，冻结前不得写死语义）**：
  1. 显式 JoinConstraint 与独立合取（跨族不相关目标）最终词表 → PR-03；
  2. UNPROBED 的分类策略（何时升级 NOT_OBSERVABLE/永不永久化）→ PR-03；
  3. 历史 witness：`GoalAchievementWitness`（单观测点完整 TRUE 的记账）结构 → PR-03 实现、PR-07 指标消费；
  4. StaticGoalCoverage 分母冻结规则（PARTIAL/不可观测排除口径）→ PR-07 冻结；
  5. fork-COW 引擎路径的 WAL 恢复语义（与输入级 SnapshotLifecycle 分开论证）→ PR-02。

### T1. SQLite/WAL 快照一致性（R7 重设计）

**〔当前行为〕**三处事实：
1. `L2Sqlite.snapshot_template`（`l2_sqlite.py:32-42`）：拷贝主 DB 文件后 **unlink `-journal/-wal/-shm`**。WAL 模式 + 活跃连接下这是数据丢失路径：已提交未 checkpoint 的页只在 `-wal` 里——拷贝缺这些页，随后 unlink 把它们从运行中 DB 也删掉。
2. `targets/vulnshop/app.py:59-62`：连接时 `PRAGMA journal_mode=DELETE` 并 **assert 非 WAL**——"L2 前提"靠靶侧断言维持，仅对 vulnshop 成立，非通用保障。
3. `SnapshotLifecycle` 还原顺序（关连接 → gc → 文件还原 → 内存钩子）对 restore 侧是正确的；风险集中在**捕获侧**（`establish_baseline` 在首输入前调用，此时应用可能持有打开的连接/未落盘的已提交帧）。

**〔方案裁决〕主选：SQLite backup API 捕获**（决策记录见 CONTRACT_DECISIONS R7）：

```python
# L2Sqlite 内部(对外表面 template_exists/restore/snapshot_template 名不变):
def snapshot_template(self, name="root") -> Path:
    # SQLite 文件(*.db):
    #   以只读连接打开 live: sqlite3.connect(f"file:{live}?mode=ro", uri=True)
    #   dst_conn = sqlite3.connect(tpl/<name>/db.tpl)
    #   src_conn.backup(dst_conn)      # 页级一致快照:
    #     - WAL 下自动含已提交未 checkpoint 的帧(backup 走页读取,不是文件拷贝)
    #     - 并发写者存在时 backup 内部重试页计数;源短时写不破坏一致性
    #     - 只读源连接 ⇒ 捕获不产生写副作用
    #   完成后**不** unlink live 的任何 sidecar(活跃连接可能正在用)
    # 非 SQLite 文件(SF_STATE_FILES 中的其他文件): 保留原位拷贝路径
    # 失败(无法打开/backup 抛错/OSError) → LifecycleError(fail-closed,带原因)
```

**前置条件（显式，写入实现 docstring 与测试）**：
- 捕获侧：live 可用只读连接打开（文件存在且非损坏）；不要求关闭应用连接、不要求 checkpoint、不要求独占。
- 恢复侧（不变，沿用现状顺序）：pre_restore_hooks 关闭应用持有的连接 → `gc.collect()` → **backup(模板→live)**（模板静止，backup 或文件拷贝均可；推荐 backup 统一路径）→ 内存钩子。恢复前 live 上**不得有活跃连接**——实现加防御检测（尝试以写模式打开并 `BEGIN IMMEDIATE` 探测，或文档化依赖既有钩子顺序 + fail-closed）。
- **禁止清单**（契约级）：无条件 `wal_checkpoint(TRUNCATE)`（需独占，有读者时 SQLITE_BUSY/阻塞，且会把副作用强加给运行中应用）；unlink 活跃连接的 `-wal`/`-shm`；在无法保证一致性时静默继续。

**fail-closed 与审计**：一致性不可保证（backup 失败、文件非 DB 且拷贝遇 IO 错、恢复时检测到活跃连接）→ `LifecycleError`，原因串进 `lifecycle_failures.jsonl`（既有审计面）。

**测试（验收用例，`tests/runtime/test_execution_context.py` 或新 `tests/runtime/test_l2_wal.py`）**：
1. WAL 模式 DB + **已提交未 checkpoint** 写入（`PRAGMA wal_autocheckpoint=0` + 写后不 checkpoint）→ establish → 破坏性写 → restore → 断言基线含该已提交数据；
2. 捕获时存在另一**活跃连接**（同进程持写连接）→ backup 仍产出一致快照（或显式 fail-closed 且带原因——实现二选一，测试按实现断言）；
3. **未提交事务**（另一连接 BEGIN + INSERT 未 COMMIT）→ 快照**不含**该数据（backup 快照读语义）→ restore 后亦无；
4. 恢复一致性：restore 后 live 与模板逻辑等价（逐表行集比较；字节级等价不作要求——backup 合法变更页布局）；
5. journal DELETE 模式回归（vulnshop 现状路径行为不变，`assert` 保留或降级为提示）；
6. 非 SQLite 声明文件走原拷贝路径回归。

**验证**：`python -m pytest stateforge/tests/runtime/ -q`

### T2. StateObservation v2 可选列（契约 §2.3）+ 观测器字段义务（§6.6.4，v1.1b）

- `state/runtime_observer.py`：`StateObservation` 追加 `subject/observed_value/value_kind/observed_at`（全缺省 None，旧构造不变）；含值谓词的 `value=True` 一致性不强制校验（旧数据兼容），仅新列携带分解结果。
- `targets/vulnshop/state_evidence.py`：三谓词族补列（`Order.status` → ENUM；`User.role` → ENUM；`Coupon.used` → BOOLEAN；`*.exists` → subject 分解）；**〔v1.1b〕orders 查询增 `user_id` 列、新增 `Order.user_id` 谓词输出（ENUM，entity=oid）**——归属关系 `Equal(Field(?order,"user_id"), Field(?user,"id"))` 运行时验证的前提（契约例 11 反例 c 的存在理由）。
- 序列化：`to_dict`/`from_dict` 透传新列（from_dict 用 `.get`，旧行 → None）。
- 测试：`tests/state/test_runtime_observer.py` 增 v2 往返、旧行读取、value_kind 词表校验、**Order.user_id 输出断言**。

### T3. 证据等级与代理独立性立法（契约 §4）

- `Evidence` 枚举追加 `STATIC_INFERRED`（现三值 → 四值；ledger `record` 只收 RUNTIME_VERIFIED 的行为不变——STATIC_INFERRED 入桶即被忽略，测试钉死）。
- 明文 R4-a/b 为契约注释 + 反例测试：构造"观测失败 + 覆盖锚点命中"场景，断言无 verified 迁移、proxy 独立存在、无降级路径（该测试在 Phase 2 迁移产生器落地后完整可跑；Phase 1 先钉 ledger/等级行为）。
- 测试：等级集合、非 verified 拒入事实桶（既有测试延续 + STATIC_INFERRED 用例）。

### T4. UNKNOWN 与 MISSING 细化（契约 §2.2/§7）

- `ValueKind` 七值词表 + `MISSING → 仅 EXISTS 判 FALSE` 判定规则落为纯函数 `eval_predicate(op, expected, observed) -> TRUE/FALSE/UNKNOWN`（放 `state/runtime_observer.py` 或新 `state/eval.py`，零依赖纯函数）。
- 观测器实体枚举失败语义：族 UNKNOWN + 失败计数（协议文档化；vulnshop `_rows` 异常路径按此标注——现状异常抛给 ExecutionContext 计数，行为已符合，补测试钉死）。
- 测试：判定规则表全分支（含 NULL/MISSING/不可比类型）；例 3 场景固化。

### T5. 映射适配器 + 可观测性两维 + 关系提取（契约 §5/§3/§6.6；v1.1b 拆 a/b）

- **T5a 映射适配器**（原 T5 内容不变）：新文件 `state/mapping.py`——`MappingStatus`/`ObservabilityStatus` 枚举、`AliasTable`（声明式别名，初版可空表直通）、`adapt_atom(atom, edge_kind, aliases) -> MappedAtom`；`*` 有类型解析按契约 §3 表；`RuntimeObserver` 协议可选属性 `subject_families`（vulnshop：Order/User/Coupon=OBSERVABLE，Session=NOT_OBSERVABLE；`NullObserver` 不变）。〔v1.1b〕SSTG 原子映射目标改为**退化关系谓词**（Field vs Const，契约 §6.6.1）。
- **T5b 关系提取器**（v1.1b 新增，契约 §6.6.2）：`state/extractor.py` 扩展——
  1. **行变量身份/provenance**：赋值追踪 `order = get_order_or_none(oid)` → 变量 →（查询函数, 表, 键参数）来源链（`SELECT * FROM orders WHERE id=?` 已解析，补"返回值→调用者变量"绑定）；dict-store 行别名（`_row_alias_map` 既有）并入同表示；`require_user()` 类认证主体函数同机制；
  2. **守卫关系反转（term 级）**：early-return `if A != B: return` ⇒ requires `A == B`（已复验的最小原型扩展：Subscript-Subscript / Subscript-Const / Attribute 形态；方向语义与 `goal_derivation.necessary_guard_edges` 同源，MVP 限 early-return 惯用法并诚实标注）；
  3. **产出**：RelPredicate（Field/Const）携带源码位置 + provenance 链（§6.6.2 三种分析能力的 MVP 面）；
  4. **`resolve`/`evaluate`**：单点快照 + 绑定求值 → TRUE/FALSE/UNKNOWN（契约 §6.6.4；evaluate_goal 原子列表泛化为 RelPredicate）。
- 测试：`tests/state/test_mapping.py`（vulnshop 黄金快照/`*` 消歧/两维九格/UNMAPPABLE 保真）+ 新 `tests/state/test_relational.py`（提取反转/provenance/resolve-evaluate 全分支，例 11 数据）。

### M0. ship_order 关系提取端到端 spike（v1.1b;v1.1c 拆分,见 §1 批次说明）

**M0-A（静态提取,项 1–3）已由关口 A 实现**（见下方实施记录）；项 4–7 属 **M0-B/M0-C**（PR-04/PR-05）。验收蓝本 = 契约例 11,不碰 20 个真实应用：

1. 从源码自动识别 `EQ(Field(?order,"user_id"), Field(?user,"id"))` 与 `EQ(Field(?order,"status"), Const("PAID"))`；
2. 追踪 `?order`/`?user` 数据来源（route 参数→查询函数→行实体；token→认证主体）；
3. 生成含实体变量与关系约束的 RelationalGoal（**完整条件合取**，非单原子挑选）；
4. 执行真实请求链（register→login→create→pay→ship）并绑定创建的 ID（复用 BindingContext/extract，绑定带来源）；
5. 从 DB 读回**单点快照**联合验证（T2 补的 `Order.user_id` 在此消费）；
6. 错误拼接否定：不同订单/不同用户/跨时间拼接 → 正确 NOT_ACHIEVED 或 UNKNOWN（例 11 反例 a/b/c）；
7. 覆盖独立确认：目标块（UPDATE 行）实际执行（TraceSession + map_trace；判达验证与覆盖验证分工，§6.6.4）。

**M0 明确不做**：`guards=False` 路径、异常/循环路径、封装谓词内联展开（`can_refund` 类走 §6.6.3 三级策略）、LLM 通道（③级仅注册接口不接模型）。
**验证**：`python -m pytest stateforge/tests/state/test_relational.py stateforge/tests/state/test_mapping.py -q` + vulnshop 临时 DB 端到端测试。

#### 实施记录（2026-10-18，关口 A 阶段 1+2 已实现）

- 已交付：`state/relational.py`（IR+结构求值器）、`state/provenance.py`（变量来源索引）、`extractor.py` 关系段（`parse_term`/`extract_guard_terms`）、`goal_derivation.py`（`StateGoal` 扩展字段 + `derive_relational_goal` 词法早退分析）、测试 `tests/state/test_relational.py` + `test_relational_extraction.py`（25 项：vulnshop 四条件/路径条件/来源链/反向分支/序列化/改名实体泛化/wrapper 条件化/不可解析/boolop 不猜）。全量 566 passed（基线 541+25），零回归。样例产物 `artifacts/m0/relational_goal.json`。
- **方向分析裁决**：`derive_relational_goal` 用**词法早退惯用法**（guard-if 全路径终止判定 + wrapper 条件化）。
- **缺陷发现（→ 已由 PR-01 修复,见下）**：`necessary_guard_edges`/`_reachable` 在**含调用函数**上静默退化——callsite→returnsite 的连接是过程间边，被 `_function_blocks` 过滤后函数子图在每个调用点断裂。

#### 实施记录（2026-10-19，PR-01 CFG 正确性已修复）

- **修复** = `_intra_adjacency` 函数内投影 CFG（`goal_derivation.py`）：为可正常返回的调用点补隐式 `callsite→returnsite` 边（builder `icfg.py:131-136` 同行同序配对，按 ID 变换定位）；被调者 AST 全路径 raise（`_all_paths_raise`）→ 不连（不可返回边界）；fall-off-end 函数隐式返回 None → 连接保留；外部调用不产 callsite 块、天然无此问题。`necessary_guard_edges` 增诚实门：基础投影图上目标不可达 → 返回空，不再把全部分支误报必要。
- **修复后实证（vulnshop ship_order UPDATE 目标）**：必要守卫 = 恰 `{(L265 err, false), (L268 order-is-None, false)}`（修复前 5 个全误报且方向恒 true）；owner/status（L271/273）正确地**不在**无条件支配集——其 guards=1 条件性由关系层（`path_conditions`）承担，两层互补经 `guard_analysis_diagnostic` 可审计。
- `derive_state_goal`（旧入口）对无调用夹具行为不变（投影只加边不删边）；`derive_relational_goal` 词法路径未动。
- 测试：新增 `tests/state/test_goal_derivation_calls.py`（8 项：vulnshop 修复验证/投影可达性/两分析一致性诊断/正常返回/never-return 不可达/fall-off-end/外部调用回归/双调用点）；`test_goal_derivation.py` 6 项、关系专项 18 项不回归。
- **残余开放项**：异常传播边（try/except）不在投影内（ICFG 本身无异常边，超出 PR-01 范围）；被调者 may-return 过近似 = 多连边方向（只可能漏报必要、不误报必要），收紧需调用图不动点精化，挂 PR-03 评估。

### T6. 持久化版本化（契约 §2.3，延续 v0）

- `state_evidence` 行级 `schema_version`（v2 行携带；v1 行按缺省识别）；`load_payload` 旧数据读取测试；PROTOCOLS.md C8 节同步 + `test_protocols.py` 用例。
- 不改 `evidence_version` 块级键语义（保持=1，块级与行级版本字段职责分开设想留实现期裁定，契约不预设）。

### T7. 审计汇总与计时细分（延续 v0，范围不变）

- recorder `observe_seconds` 可选列（C9 演进）；epoch/group_summary 汇总 `lifecycle_failures`/`observer_failures` 可选键（G10）。
- 测试：失败路径 → 键出现；正常路径 → 键不出现。

### 验收门（Phase 1，修订）

1. `python -m pytest stateforge/tests/ -q` 全绿（≥541 基线不降）；
2. **T1 全部 6 项 WAL 测试通过**（含已提交未 checkpoint 数据不丢失——v0 版未覆盖的正确性缺口）；
3. 实体/身份/作用域不错误合并：T2/T4 判定规则测试 + 例 2/例 3 固化；
4. 静态推断不自动升级：T3 ledger 拒收测试 + T5 适配器输出 evidence 恒 STATIC_INFERRED（映射产物语义身份，升级仅经运行时对账，Phase 2 消费）;
5. 同输入同基线可复现：T1 一致性测试 + 既有 lifecycle 测试；
6. 旧实验模式回归：`tests/evaluation/` + `test_default_execution_keeps_legacy_handle_behavior`；
7. **新增（R5 立法）**：任何新增指标代码不得出现"实例计数 / 静态目标数"形态——以例 7 反例测试钉死（Phase 2 指标落地时执行，Phase 1 先在契约测试中固化公式断言的纯函数形态）；
8. **新增（评审轮）**：UNVERIFIABLE ≠ 规划失败——`classify_achievement` 推导表钉死（例 10 场景：NOT_OBSERVABLE 目标 + proxy 证据 → PROXY_PROGRESS；无 proxy → UNVERIFIABLE；二者均不进失败统计）；BindingEnv 接口含显式 `JoinConstraint`（例 9 场景：缺 join 时跨族拼接不得判 ACHIEVED——接口存在性与缺省规则可覆盖性测试）。
9. **新增（v1.1b;v1.1c 改口径）**：M0 拆分后——**M0-A（项 1–3）已过**（关口 A，25 专项测试）；**M0-B（项 4–7 的观测/联判/覆盖确认）在 PR-04 验收**（依赖 PR-02+PR-03）；**M0-C（自动规划/冷启动重放）在 PR-05 验收**。提取产物无任何手写 vulnshop 归属规则的硬约束对三者全程适用。

---

## 2. Phase 2–5 任务调整摘要（同步契约）

- **Phase 2**：逐请求观测（契约 §10/§8.3 顺序）+ verified 迁移产生器（§8.1 四条件 + §8.2 失败事件）+ 状态新颖度进种子保留（消费 T2 v2 列算 InstanceStateNovelty，开关控制）+ 请求级 error 记录（演进键）。
- **Phase 3**：`StateFrontier` 目标对象含 `(atoms, binding_vars, mapping_status, observability, achievement_status, attempts, plan_signatures)`；判达走 `evaluate_goal` + `classify_achievement`（契约 §6.3/§6.5，BindingEnv 候选来源三种、显式 JoinConstraint）；**UNVERIFIABLE/PROXY_PROGRESS 目标不进失败重规划路径**（例 10）；失败前置条件/实际到达状态/计划签名入审计（§6.4 冲突 + G5）；成功链（VERIFIED_ACHIEVED，或 NOT_OBSERVABLE 目标的 PROXY_PROGRESS 链）经 `inject_checkpoint_seed` 回注，两者 origin 标注区分。
- **Phase 5**：H 系列指标按契约 §9 四类公式实现；`proxy_derived_transitions` 更名双写（`state_arm_report`）。
- **Phase 4**：不变（物理检查点评估），前置成本计时列由 T7 提供。

---

## 3. 兼容策略（延续 v0，追加两条）

v0 五条延续（开关矩阵/协议演进纪律/词表兼容/组名扩展/公平性延续），追加：

8. **指标口径立法**：R5 四类分离为协议级约束；新指标代码评审检查分母同构性（实例级分子不得配目标级分母）。
9. **观测器能力登记为可选协议**：`subject_families` 缺省 {} = UNPROBED 全体，旧观测器（含 NullObserver）零改动兼容；NOT_OBSERVABLE 与 UNPROBED 在指标中分别报告（不可混淆"显式无真源"与"没问过"）。

---

## 4. 测试计划（修订摘要）

v0 表延续，关键增补：

| 层 | 新增 |
|---|---|
| runtime | T1 六项 WAL/backup 用例；活跃连接恢复防御 |
| state | `test_mapping.py`（黄金快照/两维九格/`*` 消歧）；`eval_predicate` 全分支表驱动（含 §11 例 1–5 数据）；`test_runtime_observer.py` v2 列 |
| 协议 | C8 行级 schema_version 迁移；C9 observe_seconds |
| 采集/实验 | 例 6/例 7 场景（Phase 2 落地时执行，Phase 1 先固化纯函数断言） |

---

## 5. Phase 1 精确修改清单（交付物四）

### 5.1 拟修改文件与接口签名草案

```python
# ① stateforge/runtime/snapshot/l2_sqlite.py —— T1
class L2Sqlite:
    def snapshot_template(self, name: str = "root") -> Path:
        """SQLite 文件走 backup API 捕获(只读源连接 → 模板库);
        非 SQLite 文件保留拷贝;失败 → LifecycleError 带原因。
        对外表面(方法名/返回值/模板布局 tpl/<name>/db.tpl)不变。"""
    # restore: 推荐同路 backup(模板→live);文件拷贝路径保留为非 DB 分支
    # template_exists: 不变

# ② stateforge/runtime/execution_context.py —— T1 配套
class SnapshotLifecycle:
    # establish_baseline/end_run 调用面不变;end_run 恢复前加活跃连接防御
    # (检测失败 → LifecycleError,原因可审计);docstring 更新前置条件清单

# ③ stateforge/state/runtime_observer.py —— T2/T3/T4
class Evidence(str, Enum):
    STATIC_INFERRED = "static_inferred"      # 新增(仅标签,不入 ledger 事实桶)
    RUNTIME_VERIFIED = "runtime_verified"    # 不变
    CONTROL_FLOW_PROXY = "control_flow_proxy"# 不变
    UNKNOWN = "unknown"                      # 不变

class ValueKind(str, Enum):                  # 新增
    ENUM/BOOLEAN/NUMBER/STRING/NULL/MISSING/UNKNOWN

@dataclass(frozen=True)
class StateObservation:                      # 4 必填不变 + 4 可选(v2)
    ... subject/observed_value/value_kind/observed_at (见契约 §2.3)

def eval_predicate(op: str, expected, observed_value, value_kind: str) -> str:
    """契约 §2.1 判定规则表 → "TRUE"/"FALSE"/"UNKNOWN"(纯函数,零依赖)。"""

class RuntimeObserver(Protocol):
    name: str
    subject_families: dict[str, str]         # 可选协议属性;缺省 {} = UNPROBED
    def observe(self) -> list[StateObservation]: ...

# ④ stateforge/state/mapping.py —— T5(新文件)
class MappingStatus(str, Enum): MAPPED/PARTIAL/UNMAPPABLE
class ObservabilityStatus(str, Enum): OBSERVABLE/NOT_OBSERVABLE/UNPROBED
@dataclass(frozen=True) class AliasTable: ...
@dataclass(frozen=True) class MappedAtom: ...   # 见契约 §5
def adapt_atom(atom: str, *, edge_kind: str | None,
               aliases: AliasTable | None = None) -> MappedAtom: ...

# ⑤ stateforge/targets/vulnshop/state_evidence.py —— T2/T5
#    三谓词族补 v2 列 + subject_families 声明(适配层知识,显式标记)

# ⑤b 判达评估接口(契约 §6,Phase 1 冻结签名、Phase 3 实现;评审轮补 joins/记账)
@dataclass(frozen=True)
class JoinConstraint:                      # 显式跨主体关联(§6.1)
    left: str; right: str; relation: str   # "entity-eq"|"value-eq"|"value-in"

@dataclass
class BindingEnv:                          # §6.1
    candidates: dict[str, set[str]]
    provenance: dict[tuple[str, str], str]
    joins: tuple[JoinConstraint, ...] = () # 缺省空 = 仅族内共享推导,非永久规则

@dataclass(frozen=True)
class GoalEvaluation:                      # §6.3(状态谓词求值层)
    status: str                            # ACHIEVED|NOT_ACHIEVED|UNKNOWN
    satisfying: tuple[dict[str, str], ...]
    per_atom: dict[str, str]
    reason: str

def evaluate_goal(atoms: list[MappedAtom], env: BindingEnv,
                  facts: "ObservationView") -> GoalEvaluation: ...

AchievementStatus = str  # VERIFIED_ACHIEVED|VERIFIED_NOT_ACHIEVED|
                         # PROXY_PROGRESS|UNVERIFIABLE|UNKNOWN(§6.5)

def classify_achievement(goal_eval: GoalEvaluation,
                         observability: str,
                         proxy_evidence: bool) -> str:
    """§6.5 推导表 → AchievementStatus(纯函数);
    UNVERIFIABLE/PROXY_PROGRESS 不进失败统计——调用方(Phase 3 前沿)策略由契约钉死。"""

# ⑤c 关系目标表示与求值(契约 §6.6,v1.1b 新增;M0/T5b 实现)
@dataclass(frozen=True)
class Field:
    var: str; name: str                     # "?order","user_id"——变量身份+字段
@dataclass(frozen=True)
class Const:
    value: object

RelPredicate = tuple[str, Field, "Field | Const"]   # op, left, right

@dataclass(frozen=True)
class VarProvenance:                       # 变量来源链(§6.6.2)
    var: str                               # "?order"
    origin: tuple[str, ...]                # ("route:oid","call:get_order_or_none",
                                            #  "sql:SELECT * FROM orders WHERE id=?")

def resolve(term, snapshot: dict, bindings: dict): ...   # → value | UNKNOWN
def evaluate(relgoal: list[RelPredicate], snapshot: dict,
             bindings: dict) -> str: ...    # "TRUE"|"FALSE"|"UNKNOWN"(单点快照)

# ⑥ stateforge/fuzzing/recorder.py —— T7(演进键 observe_seconds,传入才出现)

# ⑦ stateforge/evaluation/experiment/epoch.py —— T7(汇总可选键;不改 _run_driver)

# ⑧ 文档:PROTOCOLS.md C8/C9 节同步;tests/test_protocols.py 用例
```

### 5.2 测试文件

| 文件 | 用例 |
|---|---|
| `tests/runtime/test_l2_wal.py`（新） | T1 六项 |
| `tests/runtime/test_execution_context.py` | 活跃连接防御、fail-closed 原因、既有回归 |
| `tests/state/test_runtime_observer.py` | v2 往返/旧行兼容/STATIC_INFERRED 拒收/value_kind 校验 |
| `tests/state/test_eval.py`（新或并入 observer 测试） | `eval_predicate` 表驱动全分支（例 1/3/5 数据） |
| `tests/state/test_mapping.py`（新） | 黄金快照/两维九格/`*` 消歧/UNMAPPABLE 保真（例 8 数据） |
| `tests/test_protocols.py` | C8 schema_version、C9 observe_seconds |

### 5.3 验收用例（映射到任务书八类正反例）

| 例 | 落点 | 断言要点 |
|---|---|---|
| 1 同订单迁移 | Phase 2 迁移产生器 + `test_eval` 数据预置 | 双端成对、历史 vs 当点 |
| 2 错误拼接 | Phase 3 `evaluate_goal`（Phase 1 先固化公式纯函数形态） | no-consistent-binding |
| 3 UNKNOWN≠FALSE | `test_eval` MISSING/枚举失败分支 + observer 计数测试 | 仅 EXISTS 走 FALSE |
| 4 绑定多候选/冲突 | Phase 3 BindingEnv（签名先定） | UNKNOWN vs NOT_ACHIEVED 边界 |
| 5 `*`≠EXISTS | `test_eval` NULL 分支 + `test_mapping` 消歧 | 判定相反 |
| 6 失败不产代理 | Phase 2 场景测试（Phase 1 钉等级行为） | 无降级路径 |
| 7 覆盖率上限 | 指标纯函数断言（Phase 2/5 实现） | 分子按目标计一次 |
| 8 映射/可观测两维 | `test_mapping` 九格 + vulnshop 声明 | Session 族 NOT_OBSERVABLE |
| 9 跨主体 join（评审轮） | BindingEnv/JoinConstraint 接口测试（签名冻结） | 缺 join 时跨族拼接不判 ACHIEVED |
| 10 UNVERIFIABLE≠失败（评审轮） | `classify_achievement` 推导表全分支 | NOT_OBSERVABLE+proxy→PROXY_PROGRESS；无 proxy→UNVERIFIABLE；均不进失败统计 |
| 11 ship_order 关系端到端（v1.1b） | `test_relational.py` + M0 端到端（验收门 9） | 无手写规则提取两关系；单点快照联判；错误拼接否定；覆盖独立确认 |

### 5.4 风险

| 风险 | 缓解 |
|---|---|
| backup API 在超大 DB 上的捕获时长 | 模板一次性建立（establish 时刻）；epoch 内无重复捕获；T7 计时列观测 |
| backup 与恢复路径混用页布局差异 | 验收用逻辑等价（行集）而非字节等价；文档明示 |
| v2 列导致旧 ledger 读者混淆 | 全可选 + `.get`；协议测试钉旧行读取 |
| `Evidence` 扩枚举意外进入 ledger | T3 拒收测试（STATIC_INFERRED/PROXY/UNKNOWN 均不入事实桶——与现状一致） |
| evaluate_goal/BindingEnv 签名在 Phase 3 变更 | 标注"草案"；Phase 3 实现时以契约 §6 验收用例为准修订并回写文档 |

---

## 6. 风险与下一阶段依赖（延续 v0 + 修订）

### 6a. 实施依赖图（v1.1c / PR-00,无循环;对应评审 PR 拆分）

```text
M0-A 关系提取原型(已完成,关口 A 25 测试)
   │
   ▼
PR-00 依赖拆分/契约登记(本文档,已完成) ── PR-01 CFG 正确性(已完成,8 测试)
   │                                        │
   └───────────────┬────────────────────────┘
                   ▼
        PR-02 可靠输入级快照(=T1:backup API/WAL/边界)
                   ▼
        PR-03 结构化事实+联合求值(=T2/T3/T4 核心;
              StateObservation v2/evaluate_goal/witness;
              T5a 映射适配器并入或紧随)
                   ▼
        PR-04 M0-B 已知链逐请求联合验证(五步链,不自动规划)
                   ▼
        PR-05 M0-C 自动规划+冷启动重放(完整关系合取进规划器)
                   ▼
        PR-06 失败约束/计划去重/自动修复
                   ▼
        PR-07 fuzz 接入+协议+无 LLM 消融(A/B/C/D 臂;T6/T7 并入)
```

顺序原则（评审§8）：先证明已知链验证正确（M0-B），再证明能自动找到链（M0-C），最后证明提升 fuzzing 效果（PR-07）。

### 6b. 原风险表（延续）

v0 表延续，修订两点：
- 逐请求观测开销：T7 细分计时先行，Phase 2 开关 + 仅序列种子逐步观测的策略不变；
- **Phase 3 依赖收紧**：evaluate_goal（R3）与映射两维（R6）是前沿对象判达的前置，Phase 1 T5 与契约 §6 签名冻结后 Phase 3 才可开工；Phase 2 迁移产生器（R4 契约）是 verified 迁移指标（R5 9.3）的前置。

---

## 7. 本轮完成声明

- 已交付：`STATE_CONTRACT.md` v1.1（R1–R7 契约修订 + 八类正反例）、`INTEGRATION_PLAN.md` v1.1（本文件：T1 重设计、T1–T7 同步、验收门、Phase 1 精确修改清单）、`CONTRACT_DECISIONS.md`（R1–R7 决策记录 + 文档 diff 摘要 + 正反例对照表）。
- 未修改任何代码、Collector、epoch、规划器、快照运行代码；A–F 实验协议不变。
- **按停止条件，本轮到此停止，等待人工验收。不得自行进入 Phase 1 编码。**

#### 实施记录（2026-10-19，PR-02 + PR-03 已实现）

- **PR-02（SQLite 基线与快照边界）**：`l2_sqlite.py` 双捕获模式——`backup`（缺省，输入级 SnapshotLifecycle：`Connection.backup()` 页级快照，WAL 已提交未 checkpoint 帧自动入模板；捕获**不删** live sidecar；恢复 = `BEGIN IMMEDIATE`(timeout=0) 持锁探针→fail-closed→backup(tpl→live)，不主动 unlink sidecar——WAL 连接在场时删除即破坏（实测 disk I/O error），垃圾 sidecar 由 SQLite 干净关闭自清理（已实证））与 `copy`（fork-COW 位级冻结语义保留；遇 WAL 显式 `SnapshotConsistencyError` fail-closed，supervisor 已切 `capture="copy"`）。`execution_context` 将 `sqlite3.Error/SnapshotConsistencyError` 纳入 fail-closed 包装。测试 `test_l2_wal.py` 10 项（任务书必测八项全覆盖 + sidecar 不删 + DELETE+EXCLUSIVE 捕获一致性）。
- **PR-03（结构化事实与联合求值）**：`StateObservation` v2 四可选列（subject/observed_value/value_kind/observed_at，旧构造与 ledger 去重零变化；C8 行级演进键已同步 PROTOCOLS.md）；vulnshop 观测器补 `User.id`/`Order.user_id` + 属性级 `subject_families` 声明（`Session.*`=NOT_OBSERVABLE，未声明属性=UNPROBED 不外推）；`relational.evaluate_relation` 增枚举完整性参数（MISSING→仅 EXISTS 判 FALSE）；新 `state/goal_evaluation.py`——`ObservationView`（单观测点视图，跨点拼接结构性排除）+ `evaluate_goal`（候选有限枚举合取、同一赋值贯穿、上限外不判 FALSE、witness 只来自单点完整 TRUE、认证主体关联显式化）+ `GoalAchievementWitness`。测试：`test_goal_evaluation.py` 13 项（任务书八反例 + witness 序列化 + 候选上限 + 认证关联 + vulnshop 观测器集成）+ observer/relational 增项 4 项。样例产物 `artifacts/m0/goal_evaluation_sample.json`（TRUE + auth_verified）。
- 全量 601 passed（574+27），零回归；A–F 默认行为不变（观测器新列仅 `--observe-state` 消费，驱动默认无观测）。
- **残余开放项**：观测点内"同一只读事务快照"（多表一致读取）在 vulnshop 逐表连接下为近似——PR-04 引入请求级观测时评估单事务化；候选枚举确定性排序按键字符串（实例数大时上限语义保守）；`families_complete/failed` 声明目前由调用方传入，观测器协议层声明随 PR-04 固化。

#### 实施记录（2026-10-19，PR-03.1 + PR-02.1 已实现）

- **PR-03.1（联合判达可靠性热修复）**：P0-A `BindingEnv`(fixed/enumerable/hints/provenance;旧参 binding_candidates=fixed 语义,fixed 不与全集并集);P0-B 认证分量只认同观测点 `RequestAuth.subject` verified 探测事实(provenance 字符串不是身份确认),`relations_truth` 与 `achievement_status` 分离;P0-C `ObservationView.from_observations` 证据过滤(仅 RUNTIME_VERIFIED、UNKNOWN 值不入、MISSING/exists=False→confirmed_absent、冲突值/跨 observed_at 混合 → ValueError);P0-D 四分量分类器 `classify_achievement`(path_conditions/unresolvable/trace 入闸;witness 携带 path_conditions)。测试重写为 26 项(四 P0 反例 + 原八反例四分量适配 + 分类器真值表 + vulnshop 集成双态:无探测 UNKNOWN/有探测 VERIFIED_ACHIEVED)。契约术语补 §6.6.6。
- **PR-02.1（快照防御补丁）**：SQLite 魔数头分类——live/模板"具头但不可开" → `SnapshotConsistencyError`(不静默退化普通拷贝);DELETE+EXCLUSIVE 捕获收紧为 fail-closed(提交中主文件可短暂不自洽,拷贝不能证明一致——原"拷贝即一致"测试期望按评审 PR-02.1 方向改判);`execution_context` 顶部 docstring 同步 backup 语义。新增 3 测试(损坏头/损坏模板/活跃读者可审计)。
- 回归:state+runtime 190 passed;全量见本轮提交信息。下一里程碑 PR-04(M0-B)。

#### 实施记录（2026-10-19，PR-04 / M0-B 已实现）

- **逐请求观测编排**：`execution_context.RequestLevelHarness`——生命周期每输入一次(begin/finish,恢复只在输入边界,序列内状态共享)、每步 before→execute→after(4xx/异常同样观测+error 记录)、观测经 untraced 窗口不进覆盖、观测器异常计数不 crash、`extra_observe(step)` 随步附加观测(认证探测以**该步请求实际携带**的凭据为准,非全局绑定)、`trace_span` 每步轨迹窗口(map_trace 事后判定目标块)。
- **fuzz_target**:`_dispatch_request(app,bindings,step)` 参数化(模块 target() 路径零变化)+ `make_instance(db_path,guards)` 独立实例工厂(guards 显式覆写不玩环境变量顺序、每测试独立 DB、实例级绑定表、reset fail-closed、observer/auth_probe)。
- **认证探测(P0-B 证据面)**:`state_evidence.observe_request_auth`——token 形态→hmac 复验(app.make_token 同径)→用户在场只读直查,全过才产 `RequestAuth.subject` verified 事实;伪造签名/匿名 → None。
- **测试 `test_relational_goal_runtime.py` 9 项**(任务书矩阵全覆盖):happy(入口 PAID+归属联判 TRUE/ship 后 SHIPPED/Trace 命中 UPDATE/四分量 VERIFIED_ACHIEVED/逐步状态 CREATED→PAID→SHIPPED);未登录 401→auth UNKNOWN 无 witness;错误身份 403→探测 alice≠bob→auth FALSE→VERIFIED_NOT_ACHIEVED;错误 oid 404→EXISTS 反证;未支付/支付不足 409→status 反证;观测器异常→UNKNOWN 不 crash(枚举完整性不谎报);输入隔离+冷启动重放×2(基线还原、extract 重建绑定);序列内共享(中途不 restore)。
- 样例产物 `artifacts/m0/step_observations.jsonl`(5 步×before/after 全量)+ `goal_evaluation.json`(入口/终态/四分量:VERIFIED_ACHIEVED)。回归:vulnshop 17 passed(1 skip 既有)、plan_executor+runtime+fuzzing 68 passed。
- **残余开放项**:观测器逐表连接的多表"同一只读事务快照"仍为逐表近似(单线程受控靶无并发窗口;通用并发场景待守卫检查点插桩);auth probe 的用户在场校验读 users 全表(小靶可接受,大表需参数化查询);错误链的 failed_constraint 已结构化,PR-06 再入调度记忆。

#### 实施记录（2026-10-19，PR-05 / M0-C 已实现）

- **关系规划器** `solving/relational_planner.py`(L3,复用不重写):消费 `derive_relational_goal` **完整关系合取**——常量关系经 SSTG requires 原子**后缀匹配**(零实体名硬编码)→ `render_seed` 反向链;跨实体归属关系 → 绑定义务(?user ≡ ?token 主体,运行时裁决);基础链 = 终结动作(目标块端点)经 `ensure_auth`+`resource_completion` 通用完备化;guards 条件化(config 行序启发,early-return 惯用法)——`runtime_path_context` 声明 False 时豁免守卫关系,**不套 guards=1 前置**。符号同一性:?token 单生产三消费、?oid 由 create extract 重建(不写死 ID);失效语义 `detect_auth_rebinding`(二次 login 标注,验证裁决);`signature` 内容哈希(PR-06 去重)。**三概念分离**:RelationalPlanCandidate(未执行)≠ VerifiedPlan(四分量后)≠ 重放检查点(复用 GoalCheckpoint,符号链=重放规则)。
- **测试**:单元 8 项(两种 guards 模式链形/合取非单原子/槽位统一/签名确定性/候选≠verified/重绑定检测/旧入口回归)+ E2E 4 案例全过(①空基线自动链→入口联判 TRUE+Trace 命中+终态 SHIPPED→VERIFIED_ACHIEVED 且 witness 在;②注入 alice 切换→403+auth FALSE→VERIFIED_NOT_ACHIEVED 不标 verified+规划层重绑定标注;③两次冷启动重放均 VERIFIED_ACHIEVED,oid 由 extract 重建,GoalCheckpoint 序列种子与链逐字节一致;④guards=0 链无 pay,空基线 ship 直接 200 且 UPDATE 覆盖)。
- 产物 `artifacts/m0/plan_candidate.json` + `replay_result.json`(两次冷重放 VERIFIED_ACHIEVED)。M0-C 验收门四案例全部通过。
- **残余开放项**:guards 条件化的行序启发仅在 early-return 惯用法内成立(文档化);候选未含动作失效的自动修复(重绑定只标注不换链——PR-06 调度记忆);规划仍单实体资源(?oid 单槽,多资源目标留 Phase 3 前沿)。
