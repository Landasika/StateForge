# 契约决策记录（CONTRACT_DECISIONS）

> Phase 0 契约修订轮交付物。逐条记录 R1–R7 的裁决、备选方案、取舍与兼容性影响；附文档 diff 摘要与八类正反例对照表。
> 依据：仓库现有数据结构（`state/runtime_observer.py`、`fuzzing/candidates.py`、`runtime/snapshot/l2_sqlite.py`、`targets/vulnshop/state_evidence.py`、`evaluation/experiment/state_arm_report.py` 等）与 `PROTOCOLS.md` 约束。所有"拟议"均未实现。

---

## R1 观测值 / 目标谓词 / 判定三分

**裁决**：三层分离——`ObservedStateFact`（观测到什么）/ `GoalPredicate`（断言什么）/ `eval()`（纯函数判定 → TRUE/FALSE/UNKNOWN）。兼容方式：`StateObservation` 四必填字段不动，追加 4 个可选列（subject/observed_value/value_kind/observed_at）；观测值从谓词串（`Order.status=PAID`）分解到 `observed_value="PAID"`；`StateKey.render()` 与 ledger 去重键 `(render, value)` 不变。

| 备选 | 取舍 |
|---|---|
| A. 并行新建"结构化事实容器"（弃） | 违反"不机械新增并行容器"任务约束；两套观测对象必然漂移 |
| **B. 原对象加可选列（选）** | 旧读者/旧数据零破坏；新消费者（Phase 2 调度器、Phase 3 判达）读结构化列 |
| C. 只改谓词串编码规范（弃） | 串承载结构信息继续歧义（R2 的 `*` 问题就是先例）；判定逻辑仍无着落 |

**关键语义裁决**：
- **MISSING ≠ NULL ≠ UNKNOWN** 三分：实体不存在（行无）→ 仅 `EXISTS` 判 FALSE，其余谓词 UNKNOWN；字段 NULL → 是观测（value_kind=NULL），TRUTHY 判 FALSE、EQ(None) 需显式；UNKNOWN → 无判定。这是"UNKNOWN 不转 FALSE"的落点。
- **历史 vs 当点**：ledger 保持 at-least-once 首见语义（覆盖类指标用）；目标判达确认必须当点重新观测（防"历史成立当作现在成立"的虚假判达）。

**兼容性影响**：vulnshop 观测器 +6 行/谓词族；ledger payload 行透传可选键（C8 演进）；无任何旧语义改变。

---

## R2 `*` 五义拆分

**裁决**：内部表示禁用裸 `*`；五概念专 token——E1 全局实体 `entity="*"`（StateKey 既有缺省，语义收窄为"无实体维度"）、E2 未绑定变量 `?o`、E3 值模式 `Pattern(ANY_SPECIFIC)`、E4 未知值 `value_kind=UNKNOWN`、E5 存在谓词 `op=EXISTS`。旧 SSTG 串**不改**（兼容输入），映射器按**边上下文**（produces/requires + 产源）有类型解析；无法判定 → PARTIAL/UNMAPPABLE 带原因，禁止猜测。

| 备选 | 取舍 |
|---|---|
| A. 改 builder 输出新记号（弃） | 破坏 SSTG 串兼容（backward/checkers/报告全在消费）；且旧产物文件失效 |
| **B. SSTG 串不动 + 映射器上下文消歧（选）** | 兼容零成本；歧义责任集中到一处（adapt_atom）；`d["kind"]` 边属性已足够消歧 |
| C. 保留 `*` 含义靠注释约定（弃） | 正是 v0 的缺陷——字符串层不可分 |

**事实依据**：`builder.py:70`（produces value=None→`*`）与 `goal_to_plan.py:325`（requires 动态 EQ→`X=*`）两源同形异义，现消费侧仅字符串相等比较——歧义真实存在，不是假设。

**E3≠E5 判定差异**（落为测试）：NULL 上 `ANY_SPECIFIC` 不匹配而 `EXISTS` 为 TRUE；MISSING 上 `EXISTS` 为 FALSE 而 `ANY_SPECIFIC` 不匹配。

**兼容性影响**：零（映射器是新增消费侧；既有消费者行为不变）。

---

## R3 联合实体绑定与目标判达

**裁决**：`BindingEnv`（变量→候选实体集，带来源与冲突记录）+ `evaluate_goal`（存在赋值 σ 使全部原子 TRUE 才 ACHIEVED）。静态原子按 `entity_kind` 生成变量，**同主体族共享同一变量**（`Order.owner`/`Order.status` 共 `?order`）——这是阻止"订单 3 owner + 订单 4 status"错误拼接的结构保证。候选来源优先级：运行时观测枚举 > 符号槽回填（复用 vulnshop `_BINDINGS`/BindingContext extract 机制，不替代）> 请求参数。冲突时运行时观测优先、槽降级 hint、冲突入审计。

| 备选 | 取舍 |
|---|---|
| A. 逐原子独立判达后取交集（弃） | 交集丢绑定同一性——order3 满足 A、order4 满足 B 会被误判达成；正是任务书反例 |
| **B. 绑定环境 + 联合求值（选）** | 一致性显式、可审计（satisfying 赋值可输出）；存在量词语义清晰 |
| C. 约束求解器（z3 等）（弃，现阶段） | 依赖与成本过重；当前实体域是有限枚举，暴力赋值 + 上限即可；留待实体关系复杂化再评估 |

**边界裁决**：
- 候选空 → **UNKNOWN**（非 NOT_ACHIEVED）：无实体 ≠ 不可满足证明；
- 全候选均有原子 FALSE → NOT_ACHIEVED；否则 UNKNOWN（三值保守传播，§7.2）；
- 单原子存在性命中与多原子联合判达**分开定义**（前者是后者的退化情形；覆盖指标按目标粒度、新颖度按实例粒度——衔接 R5）。

**兼容性影响**：新纯函数（state 域）；`reached_atoms`/`reached_state_atoms` 现行为不动（proxy 桶延续）；规划执行层符号槽机制复用不改动。

---

## R4 观测失败 ≠ 代理证据

**裁决**：四条硬规则入契约 §4/§8：(a) proxy 只能源于独立控制流/覆盖分析，禁止由观测失败降级产生；(b) 双来源可关联呈现但分桶存储、各带 via 与置信边界；(c) verified 迁移四条件（双端观测成功 + 双端 verified + 值真实变化 + span 完整）缺一不可，失败记 `TransitionAttempt(status=unknown, cause=...)`；(d) 请求异常/HTTP 错误**不跳过 after 观测**（副作用可能已发生），错误与迁移同事件携带可筛。

| 备选 | 取舍 |
|---|---|
| A. 观测失败时自动降级 proxy 记账（弃） | 制造虚假判达通道；违背"UNKNOWN≠FALSE"同源原则 |
| **B. 失败即 UNKNOWN 事件 + 独立 proxy 通道（选）** | 证据纯度可审计；关联呈现满足分析需求 |
| C. 观测失败直接丢弃该输入全部证据（弃） | 丢 before 端已成功事实；且掩盖可观测性质量问题 |

**顺序契约**（错误+副作用）：execute（异常记录不吞——现状 `candidates.py:71-74` 吞掉为**当前行为**，拟议加记录）→ after-observe（untraced）→ 序列末 lifecycle 还原（fail-closed 沿用）。HTTP 4xx/5xx 与异常同处理（响应错误≠无副作用）。

**兼容性影响**：Phase 2 落地项；Phase 1 先钉 ledger 等级行为；collector 错误字段为演进增列，legacy 路径不变。

---

## R5 覆盖率与新颖度四类分离

**裁决**：四指标独立口径——① StaticGoalCoverage（目标粒度，分子=achieved 目标数——**多原子须同绑定**、按目标计一次，分母=G_observable，结构性 ≤100%）；② InstanceStateNovelty（首见 (subject,entity,observed_value,value_kind) 计数/曲线，**无静态分母**）；③ VerifiedTransitionNovelty（§8.1 迁移计数；覆盖率仅当分母=双端可观测静态迁移候选闭包时可报，缺省只报计数）；④ ProxyProgress（现状口径独立分桶）。PARTIAL/NOT_OBSERVABLE/UNPROBED/UNMAPPABLE 目标不入 ① 分母，各自单列计数。

| 备选 | 取舍 |
|---|---|
| A. 实例展开 verified 数 / 静态原子数（弃，即任务书禁的形式） | 10 实例 × 1 目标 = 1000% 的量纲错误；实例努力与目标达成混同 |
| **B. 目标粒度覆盖率 + 实例粒度新颖度并报（选）** | 两类问题（效果评估 vs 探索进展）各自正确量纲；反例 7 结构性排除 |
| C. 只报计数不报任何覆盖率（保守备选） | 放弃 H1/H3 臂需要的可比率；①的分母在 G_observable 上良定义，不必放弃 |

**事实依据**：现状指标全部是计数（`state_arm_report.py` len 并集，无比率），无存量违规；`transitions_observed` 实为静态边×代理判达派生量——契约要求更名 `proxy_derived_transitions`（旧键保留双写）。

**兼容性影响**：报告层加新键不改旧键；H 系列实现（Phase 5）按此口径。

---

## R6 映射完整性 × 可观测性两维正交

**裁决**：`MappingStatus`（MAPPED/PARTIAL/UNMAPPABLE——静态原子→规范化语义的**语义**完整性）与 `ObservabilityStatus`（OBSERVABLE/NOT_OBSERVABLE/UNPROBED——运行时有无真源）为独立维度；观测器可选协议属性 `subject_families` 声明主体族状态（缺省 {} = UNPROBED，`NullObserver` 零改动）。`Session.authed_as=<authed>`@vulnshop = MAPPED ∧ NOT_OBSERVABLE：语义目标仍可规划消费，只是运行时不可验证、不入 verified 覆盖分母。

| 备选 | 取舍 |
|---|---|
| A. 单一 MAPPABLE 布尔（v0 草案倾向，弃） | 语义失败与观测缺失混同；Session 目标会被误标"不可映射"而逐出规划面 |
| **B. 两维正交 + 观测器登记（选）** | "能规划"与"能验证"解耦；UNPROBED≠NOT_OBSERVABLE 保留"没问过"与"问了没有"之别 |
| C. 可观测性由靶名/启发式推断（弃） | 适配层知识必须显式声明（vulnshop 观测器 docstring 既有纪律的推广） |

**兼容性影响**：协议可选属性 + vulnshop 声明约 4 行；指标分母规则由此定义（§9.1）。

---

## R7 SQLite/WAL 快照一致性

**裁决**：**主选 SQLite backup API**：`L2Sqlite.snapshot_template` 对 `*.db` 改为"只读源连接 → `src.backup(dst_conn)` 页级快照"，捕获后**不** unlink live sidecar；恢复推荐同路 backup（模板静止）。非 SQLite 声明文件保留拷贝路径。一致性不可保证 → `LifecycleError` fail-closed 带可审计原因。**禁止**：无条件 `wal_checkpoint(TRUNCATE)`；unlink 活跃连接的 `-wal`/`-shm`；静默继续。

| 备选 | 取舍 |
|---|---|
| A. 无条件 wal_checkpoint(TRUNCATE)（弃） | 需独占；有活跃读者时 SQLITE_BUSY/阻塞；向运行中应用强加副作用（可能恰在其事务中间）——任务书明令禁止 |
| **B. backup API 页级快照（选）** | WAL 已提交未 checkpoint 帧自动包含；并发写者下内部重试；只读源零写副作用；未提交事务天然不进快照（快照读语义） |
| C. 安全静默点捕获（关全部连接+journal 落盘后拷贝）（备选，部分场景） | 语义最简单但要求"能关掉所有连接"——对持有长连靶不通用；作为非 DB 文件与小靶快速路径保留 |
| D. 现状（文件拷贝+删 sidecar+靶侧 assert DELETE）（弃为通用方案） | `app.py:59-62` 的 assert 证明"DELETE 前提"只对 vulnshop 成立；WAL 靶下现状是**数据丢失**路径（拷贝缺已提交 WAL 页且 unlink 毁掉它们） |

**前置条件**（入实现 docstring 与测试）：捕获=live 可只读打开（不要求关连接/checkpoint/独占）；恢复=先关应用连接（pre_restore_hooks）+gc（既有顺序），恢复前无活跃写连接（加防御检测，失败 fail-closed）。

**测试覆盖**（任务书要求项）：WAL 已提交未 checkpoint 写入存活于基线；捕获期活跃连接；未提交事务不进快照（正常语义）；恢复后逻辑等价；DELETE 模式回归；非 DB 文件回归。

**兼容性影响**：`L2Sqlite` 对外表面（方法名/模板布局）不变；vulnshop 断言可保留（DELETE 快路径）或统一走 backup（推荐，消除双路径）；恢复等价判据从字节级放宽为逻辑级（backup 合法变更页布局），文档明示。

---

## 评审轮补充决策（D1/D2，2026-10-18 人工评审反馈落地）

### D1. 联合绑定的跨主体关联（R3 补充）

**评审意见**：`Order.*` 共用 `?order` 只能防同族拼接；跨主体约束（如订单 owner 必须等于某 admin 用户的 username）推导不出来，主体族不得作为永久固定关联规则。

**裁决**：BindingEnv 增加显式 `JoinConstraint(left, right, relation ∈ {entity-eq, value-eq, value-in})`；§6.2 的族内共享降格为**缺省推导启发**（映射期生成，Phase 1 只实现它），跨族一致性必须显式声明 join，求值器对缺省与显式一视同仁。例 9 固化：缺 join 时 `Order.owner(order:7)` 与 `User.role(user:bob)=admin` 不得联合判达。

| 备选 | 取舍 |
|---|---|
| A. 族共享作为硬编码永久规则（弃） | 跨族关联表达不了；且把启发固化进判定器，后续目标形态扩展（多用户/多资源）要改核心 |
| **B. 缺省推导 + 显式 JoinConstraint 并存（选）** | Phase 1 实现面不变（缺省即现状规划），接口先冻结；Phase 3 前沿目标构造方按需加 join |
| C. 全部 join 显式化、无缺省（弃，现阶段） | 单族目标（绝大多数）全部要手写 join，噪声大；缺省覆盖 90% 场景 |

**兼容性影响**：签名层（`joins` 字段缺省空元组），零行为差异；例 9 进接口测试。

### D2. AchievementStatus 四值记账与 UNVERIFIABLE≠失败（R6/R3 补充）

**评审意见**：不可观测目标（vulnshop 登录后的 `Session.authed_as`）不能直接视为规划失败，否则前沿会反复规划已经成功的登录链；需区分 VERIFIED_ACHIEVED / PROXY_PROGRESS / UNVERIFIABLE / VERIFIED_NOT_ACHIEVED。

**裁决**：新增契约 §6.5 `AchievementStatus ∈ {VERIFIED_ACHIEVED, VERIFIED_NOT_ACHIEVED, PROXY_PROGRESS, UNVERIFIABLE, UNKNOWN}`（第五值 UNKNOWN = 可观测但观测失败/候选不足的瞬态，与 UNVERIFIABLE 的能力缺失区分）。推导表 `classify_achievement(goal_eval, observability, proxy_evidence)` 纯函数落地；关键策略规则：UNVERIFIABLE 不进失败统计、不触发重规划；PROXY_PROGRESS 是 NOT_OBSERVABLE 目标的最高可得记账（可推进、永不升 VERIFIED）；VERIFIED_NOT_ACHIEVED 需可靠观测反证（与"没观测到达成"严格区分）。例 10 固化反例（禁止 UNVERIFIABLE→失败→无限重规划循环）。

| 备选 | 取舍 |
|---|---|
| A. 沿用 ACHIEVED/NOT_ACHIEVED/UNKNOWN 三值（弃） | NOT_OBSERVABLE 目标永远 UNKNOWN/未达 → 正是评审指出的重规划循环 |
| **B. 五值记账 + 推导表纯函数（选）** | 能力缺失（UNVERIFIABLE）、瞬态观测失败（UNKNOWN）、可靠反证（VERIFIED_NOT_ACHIEVED）、代理进展（PROXY_PROGRESS）四类边界清晰；评审四值全部覆盖且多出的 UNKNOWN 有独立必要 |
| C. 把 proxy 证据并入 goal_eval 三值里（弃） | 混淆求值层（状态谓词真假）与记账层（证据+能力），R4 分桶纪律会被侵蚀 |

**兼容性影响**：`classify_achievement` 新纯函数（Phase 1 冻结签名、测试先行，Phase 3 前沿消费）；验收门 8 新增；`reached_atoms` 现行为不动。

### 采纳的执行批次（记录性）

评审建议 Phase 1 分三批编码：**T1+T2 →（验收）→ T3+T4+T5 →（验收）→ T6+T7**。已写入 INTEGRATION_PLAN §1 批次说明；风险最大的底层修改（WAL/结构化列）先行隔离。

---

## 评审轮二补充决策（D3–D5，2026-10-18 关系语义反馈落地）

> 触发：评审指出最难点不是保存状态，而是**自动发现状态间语义关系**（如 `order["user_id"] != user["id"]` 的归属约束），并给出 provenance → relational constraints → runtime verification 三步路线。以下决策全部先经仓库事实核查。

### 事实核查（决策依据，2026-10-18 实测）

1. **当前 extractor 对 `ship_order` 两守卫零产出**：`extract_state_facts(vulnshop)` 在 `ship_order` 只产 2 条 WRITE（行 201/275 的 UPDATE SQL 解析）；行 271 `order["user_id"] != user["id"]` 与行 273 `order["status"] != "PAID"` **无任何 StateFactCandidate**——`_extract_db` 守卫分支只认 `obj.attr` 形态、`_extract_dict_store` 只认 `_DB` 容器别名，SQL 行变量两边都不匹配。评审"现有提取对双动态变量比较/字典下标/跨函数来源局限"的判断属实。
2. **最小原型复验通过**：early-return NotEq 反转 + `ast.unparse` 在真实 `ship_order()` 上提取出 `order['user_id']==user['id']`（行 271）与 `order['status']=='PAID'`（行 273）——评审的可行性主张可复现。
3. **观测器缺归属字段**：vulnshop 观测器 orders 查询只取 `id, status`（`state_evidence.py:79-83`），无 `user_id`——归属关系的运行时验证今天不可能，评审增补项确认。

### D3. 关系约束目标模型（RelationalGoal）

**裁决**：目标表示从单主体谓词推广为 **Term 关系谓词**（`Field(var,name)` / `Const(value)` / `RelPredicate(op,left,right)`），落契约 §6.6；`StructuredPredicate` 与 SSTG 原子均为退化特例（Field vs Const），经适配器映射、不建并行表示。**双轨分工**：SSTG 仍是规划动作链词表（backward 零改动），RelationalGoal 是判达与验证的目标表示。变量身份必须携带 provenance 链（route 参数→查询函数→行实体；token→认证主体），禁止裸字符串变量名。

| 备选 | 取舍 |
|---|---|
| A. 为每类业务关系手写规则（弃） | 失去通用性——评审开篇即排除 |
| **B. 比较操作本身 + 数据流身份（选）** | `!=` 早退反转即精确关系约束，无需理解"归属"概念；provenance 保证 `?order` 是真查询行 |
| C. LLM 直接产业务语义（弃为主通道） | 无源码依据的语义不可信；降级为③级候选通道（D4） |
| D. 直接改 StructuredPredicate 支持任意表达式树（弃） | 表达力失控、判定器复杂化；受限 Term 词表（Field/Const + 六算子）覆盖目标场景且可判定 |

### D4. 三级语义发现机制与 LLM 边界

**裁决**：①静态（数据流+条件比较+SQL/ORM：可靠，产物 STATIC_INFERRED 附 provenance）→ ②动态（执行+只读观测：可靠，RUNTIME_VERIFIED）→ ③LLM（**仅候选**，STATIC_INFERRED 子标签 `llm-proposed`，必须经②验证否则保持 PARTIAL/UNKNOWN，永不自动升级）。封装谓词（`can_refund(user,order)` 类）处理顺序：函数体分析 → 读面追踪 → 运行时观测/插桩 → LLM 候选 → 仍无法解释则明确停止（UNKNOWN）。系统目标是"逐步收集可靠关系、不确定时停止"，不是"完全理解程序"。

| 备选 | 取舍 |
|---|---|
| A. 全静态（不做③）（保守备选） | 封装/间接依赖永远 UNKNOWN；③保留候选通道成本低、有验证闸门 |
| **B. 三级 + 闸门（选）** | 每级可靠度与产物等级绑定；LLM 无法污染证据链 |
| C. LLM 主导 + 静态验证（弃） | 颠倒信任方向；候选爆炸且验证负担全落运行时 |

### D5. 表示层统一：关系原子 vs JoinConstraint vs SSTG 原子

**裁决**：三者归一到关系谓词层——`Equal(Field(?o,f1), Field(?u,f2))` **本身就是** D1 的 value-eq join；故 **JoinConstraint 词表收窄**为 `entity-eq`（两变量同实体）与 `value-in`（集合成员），value-eq 由关系原子表达（目标含关系原子时禁止叠加同义 join，防双轨）。SSTG 原子经 T5a 适配为退化关系谓词（Field vs Const）。`evaluate_goal` 原子列表泛化为 RelPredicate 列表，联合判达/同一 σ 语义不变。

| 备选 | 取舍 |
|---|---|
| A. 关系原子与 JoinConstraint 并存双轨（弃） | 同一约束两种写法，必然漂移；例 9 的 join 在新表示下就是关系原子 |
| **B. 关系原子为权威、join 收窄（选）** | 单一权威表示；D1 接口投资保留（entity-eq/value-in 仍有独立价值） |
| C. 全面替换 SSTG 为关系图（弃） | backward/报告/消融全在 SSTG 上；规划词表不需要实体维度 |

### 落地与批次影响

- T2 增补：vulnshop 观测器补 `Order.user_id`（事实 3 的修复）；T5 拆 T5a（适配器）/T5b（关系提取：行变量身份/守卫反转/resolve-evaluate）；**M0 spike**（ship_order 端到端，例 11 七项）成为第二批开工条件（验收门 9）——评审"先做一个非常小的验证，不碰 20 个真实应用"的直接落地。
- 兼容性：StructuredPredicate/SSTG/JoinConstraint 接口保留（词表收窄属窄化非破坏）；vulnshop 观测器加字段为 C8 演进键自然扩展；零旧行为破坏。

---

## 文档 diff 摘要（v0 → v1.1）

### STATE_CONTRACT.md（v1.1 → v1.1a，评审轮增补以〔评审轮〕标注）

| 节 | 变更 |
|---|---|
| §6.1 | **增补**：BindingEnv 增 `joins` 字段 + `JoinConstraint`（评审轮，D1） |
| §6.2 | **修订**：族内共享从"必须"降格为"缺省推导启发，非永久规则"；跨族例 G2（D1） |
| §6.5 | **新增**：AchievementStatus 五值记账 + 推导表 + 前沿策略（D2） |
| §11 | **新增**：例 9（跨主体 join）、例 10（UNVERIFIABLE≠失败）（评审轮补充） |
| §12 | 增 §6/§6.5 两行三段标注 |

（首次 v1.1 的 §2–§5/§7–§10/§11 例 1–8/§12 变更见下表，均维持。）

#### v0 → v1.1（首次修订）

| 节 | 变更 |
|---|---|
| §2 | **重写**：v0 的 `StateFact.value ∈ True/False/UNKNOWN` 三值限制废除，改为观测/谓词/判定三分 + ValueKind 七值类型学 + `StateObservation` v2 可选列兼容适配（当前行为/拟议/迁移成本标注） |
| §3 | **新增**：`*` 五义拆分 token 表 + SSTG 兼容输入的边上下文消歧表（v0 无此节，`*` 歧义未处理） |
| §4 | 追加 R4-a（代理独立性）/R4-b（关联不合并）硬规则 |
| §5 | **重写**：v0 的映射单维（MAPPED/PARTIAL/UNMAPPABLE 含可观测性混淆）改为两维正交 + `subject_families` 登记 + 九格组合表；`MappedAtom` 签名（pattern/needs_binding/cause 新字段） |
| §6 | **新增**：BindingEnv/evaluate_goal/联合判达/候选优先级/冲突裁决（v0 完全没有绑定概念） |
| §7 | MISSING 细化（枚举失败 vs 行不存在分开）+ UNKNOWN 传播规则 |
| §8 | **重写迁移契约**：四条件 verified 迁移、失败 TransitionAttempt、代理隔离、错误+副作用观测顺序（v0 仅"前后可靠"一句） |
| §9 | **重写指标**：v0 的"verified 状态覆盖按实体实例展开/适配原子数"（即任务书禁止的形式）改为四类分离公式 + PARTIAL/不可观测处理 + 反例 7；`transitions_observed` 更名标注 |
| §10 | 并入错误路径时序 |
| §11 | **新增**：八类正反例（任务书第三节全量） |
| §12 | 落地对照表改为三段标注（当前行为/拟议契约/迁移成本） |

### INTEGRATION_PLAN.md（v1.1a，评审轮增补）

| 节 | 变更（评审轮） |
|---|---|
| §1 批次说明 | **新增**：三批执行（T1+T2 → T3+T4+T5 → T6+T7），每批验收 |
| 验收门 | **新增第 8 条**：UNVERIFIABLE≠失败推导表钉死；JoinConstraint 接口存在性 |
| §5.1 | **增补**：JoinConstraint/BindingEnv.joins/GoalEvaluation/classify_achievement 签名草案（⑤b） |
| §5.3 | **新增**：例 9/例 10 验收用例行 |
| §2 Phase 3 | 判达改走 evaluate_goal + classify_achievement；achievement_status 入前沿对象；回注分 origin |

#### v0 → v1.1（首次修订）

| 节 | 变更 |
|---|---|
| T1 | **重设计**（R7）：现状三事实（sidecar unlink 风险/靶侧 assert/restore 顺序正确）→ backup API 主选 + 备选对比 + 禁止清单 + 前置条件 + 六项测试 |
| T2 | 对接 v2 可选列（v0 仅"StateKey 规范化"） |
| T3 | 从"标签化"升级为 R4-a/b 立法 + 反例测试 |
| T4 | MISSING/NULL/UNKNOWN 细化 + `eval_predicate` 纯函数 |
| T5 | 扩为映射 + 可观测性两维 + `*` 消歧（v0 仅适配器） |
| T6/T7 | 基本延续；T6 明确行级 schema_version 不动块级 `evidence_version` 语义 |
| 验收门 | 增第 7 条指标口径立法；T1 六项测试列为门 |
| §2 | Phase 2–5 按契约同步（StateFrontier 判达走 evaluate_goal 等） |
| §3 | 兼容策略追加两条（指标立法/观测器登记可选协议） |
| §5 | **新增**：Phase 1 精确修改清单（文件×8、签名草案、测试文件×6、验收用例映射、风险表） |

### 新增 CONTRACT_DECISIONS.md（本文件）

---

## 八类正反例对照表（v1.1a：含评审轮补充例 9/10）

| # | 场景 | 契约条款 | 文档位置（例） | Phase 1 测试落点 | 判定期 |
|---|---|---|---|---|---|
| 1 | 同一订单 PENDING→PAID→CANCELLED 观测与迁移 | §8.1/§2.1 | STATE_CONTRACT §11 例 1 | `test_eval` 数据预置 | 判定规则 Phase 1；迁移产生 Phase 2 |
| 2 | 不同订单 owner/status 错误拼接（反例） | §6.3 | §11 例 2 | evaluate_goal 公式断言（纯函数形态） | Phase 1 固化公式，Phase 3 实现判定 |
| 3 | 观测失败/枚举失败 UNKNOWN≠FALSE | §2.1/§2.2/§7 | §11 例 3 | `test_eval` MISSING 分支 + observer 计数 | Phase 1 |
| 4 | 未绑定变量/多候选/绑定冲突 | §6.2–6.4 | §11 例 4 | BindingEnv 签名草案（§5.1） | Phase 3（Phase 1 定签名） |
| 5 | `*` 通配值 ≠ EXISTS（NULL 判定相反） | §3-E3/E5 | §11 例 5 | `test_eval` NULL 分支 + `test_mapping` 消歧 | Phase 1 |
| 6 | 观测失败不产代理迁移 | §4 R4-a/§8.2 | §11 例 6 | 等级行为钉死（T3） | Phase 1 钉行为，Phase 2 场景测试 |
| 7 | 10 实例命中 1 目标覆盖率 ≤100% | §9.1 | §11 例 7 | 指标公式断言（验收门 7） | Phase 2/5 实现 |
| 8 | Session 可映射但不可观测 | §5 两维 | §11 例 8 | `test_mapping` 九格 + vulnshop `subject_families` | Phase 1 |
| 9 | 跨主体关联需显式 join（族共享推导不出）（评审轮） | §6.1/§6.2 | §11 例 9 | BindingEnv/JoinConstraint 接口测试（验收门 8） | Phase 1 冻结签名，Phase 3 求值实现 |
| 10 | UNVERIFIABLE≠规划失败（防登录链无限重规划）（评审轮） | §6.5 | §11 例 10 | `classify_achievement` 推导表全分支（验收门 8） | Phase 1 纯函数，Phase 3 前沿消费 |
| 11 | ship_order 关系目标端到端（无手写归属规则）（评审轮二） | §6.6 | §11 例 11 | `test_relational.py` + M0 spike（验收门 9） | 第二批 T5b + M0 |

---

## 停止声明

本轮（v1.1b，评审轮二"关系语义"反馈落地）仅修订架构文档与契约设计：STATE_CONTRACT.md（§2.1 expected 泛化注记、§6.1 join 词表收窄、§6.6 关系约束目标、§12 表增行、例 11）、INTEGRATION_PLAN.md（T2 观测器字段义务、T5 拆 a/b、M0 spike 与验收门 9、签名草案 ⑤c）、本文件（事实核查三项 + D3/D4/D5）。未修改任何核心代码；A–F 实验协议与缺省行为不变。**完成即停，等待 Phase 1 编码指令（建议第一批 T1+T2 起步，M0 作第二批开工条件）。**
