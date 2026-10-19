# 状态语义统一契约（v1.1，修订版）

> Phase 0 契约修订轮交付物。v1.1 按《Phase 0 契约修订任务》R1–R7 修订 v0 草案；修订决策记录见 `CONTRACT_DECISIONS.md`。
> 本文是**契约设计**，不是实现描述。凡与现有实现不一致处，用 **〔当前行为 / 拟议契约 / 迁移成本〕** 三段标注，不得把拟议行为当作已实现。
> 纪律：遵守 PROTOCOLS.md 演进级纪律（只加不改、`.get` 缺省读、未来实现时同步 `test_protocols.py`）；`StateKey.render()`、C8 `state_evidence` v1 键集、C9 快照键、C10 steps 语义不破坏。

---

## 1. 词表盘点与裁决（延续 v0）

三套现存词表（W1 SSTG 原子 / W2 M1 抽象键 / W3 StateKey）的事实盘点不变（见 `STATEFORGE_AUDIT.md` §2）。裁决延续并细化为：

- **统一骨架** = 谓词级结构化事实（§2），带实体绑定（§6）与独立可观测性维度（§5）；
- W1 作为**目标词表**经映射适配器（§5）进入统一骨架，`*` 等多义记号按 §3 有类型解析；
- W2 收窄为迁移发现内部表示，不参与对账；
- `StateRegistry`（节点/边去重容器）、`TransitionRecorder`（会话边界 + diff 引擎）、`StateEvidenceLedger`（verified 事实台账）职责边界保留——扩展均为"新增可选字段/新增方法"，不建并行容器。

---

## 2. 状态事实模型：观测值、目标谓词、判定三分（R1）

### 2.1 三层概念（强制区分，不得混用）

```text
① ObservedStateFact（观测事实）
     (subject, entity, scope, observed_value, value_kind, provenance)
     例:(Order.status, order:3, resource, "PAID", ENUM, db:orders@req-7)
     语义:某时刻某实体的某属性观测值是什么。没有真/假,只有"观测到什么"。

② GoalPredicate（目标谓词,静态或动态来源）
     (subject, entity_var, op, expected)
     例:(Order.status, ?o, EQ, "PAID")
     语义:对某(待绑定)实体断言什么。没有观测,只有断言。
     〔v1.1b〕expected 可为 Const **或 Field**(跨实体关系形态),见 §6.6——
     关系谓词是本表示的推广,不是并行新表示。

③ Evaluation（判定,纯函数）
     eval(②, ①们, 绑定σ) → TRUE | FALSE | UNKNOWN
     例:eval(EQ"PAID", fact(PAID)) = TRUE
        eval(EQ"SHIPPED", fact(PAID)) = FALSE     # 同实体有值观测,极性可判
        eval(EQ"PAID", 实体缺失)     = UNKNOWN    # 见 2.4-MISSING
```

**判定规则表**（op × 观测形态 → 结果；缺省其余 = UNKNOWN）：

| op \ 观测 | 有值（ENUM/BOOL/NULL 见行内） | MISSING（实体不存在） | 无观测/观测失败 |
|---|---|---|---|
| EQ(v) | observed==v → TRUE；observed≠v（可比较）→ FALSE；类型不可比 → UNKNOWN | UNKNOWN | UNKNOWN |
| NE(v) | 对偶（==→FALSE，≠→TRUE） | UNKNOWN | UNKNOWN |
| TRUTHY | bool(value)=True→TRUE；False/NULL→FALSE | UNKNOWN | UNKNOWN |
| FALSY | 对偶 | UNKNOWN | UNKNOWN |
| EXISTS | value_kind≠MISSING → TRUE | FALSE（**唯一 MISSING→FALSE 通道**） | UNKNOWN |
| IN/NOT_IN | 成员判定同 EQ/NE | UNKNOWN | UNKNOWN |

**历史语义 vs 当点语义**：`StateEvidenceLedger` 是**首见历史台账**（`(render,value)` 去重，`runtime_observer.py:182`）——它证明"某事实至少成立过一次"（at-least-once）。覆盖类指标用历史语义（§9）；**目标判达确认**（Phase 3 规划链执行后的 verify）必须用**当点语义**：判达时刻由观测器重新读回，不得以台账历史代替。两种语义在指标名中显式区分（`achieved_at_least_once` / `achieved_at_check`）。

### 2.2 值类型学（R1：枚举、布尔、缺失、空值、序列化）

`ValueKind ∈ { ENUM, BOOLEAN, NUMBER, STRING, NULL, MISSING, UNKNOWN }`：

- **ENUM/NUMBER/STRING**：观测原值，JSON 原生序列化（数字保持数字，不字符串化）。
- **BOOLEAN**：真观测布尔（如 `Coupon.used`）。注意与 `StateObservation.value`（谓词极性，§2.3）不同字段。
- **NULL**：字段值为 SQL NULL / Python None——**是观测结果**（`observed_value=None, value_kind=NULL`），与 UNKNOWN（没观测到）用 `value_kind` 区分，禁止用哨兵字符串。
- **MISSING**：实体不存在（orders 表无 id=3 的行）。此时 `Order.exists(3)` 可判 FALSE；**其余属性谓词一律 UNKNOWN**（不存在的东西没有 status——禁止把缺失当空串/False 比较出 FALSE）。
- **UNKNOWN**：无观测/观测失败/值不可比。

### 2.3 兼容适配：`StateObservation` / `StateKey` / `StateEvidenceLedger`（不建并行容器）

〔当前行为〕`StateObservation = (key, value: bool, evidence, source)`；vulnshop 观测器把观测值嵌进谓词串（`Order.status=PAID{3@resource}`, `value=True`），或裸谓词 + 布尔观测（`Coupon.used{code}`, `value=bool(used)`）；ledger 去重键 `(render, value)`。

〔拟议契约〕**四必填字段与语义不变**，追加 v2 可选列（演进键；旧读者 `.get` 忽略）：

```python
@dataclass(frozen=True)
class StateObservation:
    key: StateKey                 # 不变;render() 兼容键不变
    value: bool                   # 不变:谓词极性(含值谓词恒 True 于该观测;裸谓词=布尔观测)
    evidence: Evidence            # 不变
    source: str = ""              # 不变
    # ---- v2 可选(拟议,全部缺省=None 表示"未提供",不改变旧语义) ----
    subject: str | None = None        # "Order.status"(分解列;None=沿用 key.predicate)
    observed_value: object = None     # 裸观测值 "PAID"/True/None
    value_kind: str | None = None     # §2.2 词表;None=未分解(旧形态)
    observed_at: int = -1             # 输入内请求序号;-1=输入级观测(现状)
```

- **类型 A（含值谓词）**：`Order.status=PAID` + `value=True` → v2 补 `subject="Order.status", observed_value="PAID", value_kind="ENUM"`。`value=True` 的一致性校验：含值谓词的 `value` 必须为 True（它描述的是"该实体取该值"这一观测本身）。
- **类型 B（裸谓词）**：`Coupon.used` + `value=bool` → v2 补 `observed_value=bool, value_kind="BOOLEAN"`。
- `StateEvidenceLedger`：**去重键与 `to_payload()` v1 键集不变**；v2 行内透传可选列（payload 行追加可选键，旧读者忽略）。结构化新颖度（§9 实例新颖度）由**调度器直接消费 observations 列**计算，不改 ledger 职责。
- `StateKey(predicate, entity, scope)` 与 `_parse_rendered` 互逆不变；`entity="*"` 语义收窄为 §3-E1（全局/无实体维度），不再兼任其他含义。

〔迁移成本〕vulnshop 观测器补三列（约 +6 行/谓词族）；ledger 序列化透传（+3 行）；无破坏性。

---

## 3. `*` 的五义拆分（R2）

**内部表示禁止裸用 `*`**；五个概念各有专 token：

| # | 概念 | 专 token / 表示 | 出现位置 | 语义 |
|---|---|---|---|---|
| E1 | 全局状态实体身份 | `entity="*"`（StateKey 既有缺省，兼容保留）；内部模型 `EntityRef(kind=global)` | Session/全局变量类事实 | 该主体没有实体维度（不是"未知实体"） |
| E2 | 未绑定实体变量 | `?o`（绑定环境 §6 中的变量） | 目标谓词、规划链符号槽 | 待绑定的实体变量，绑定前不可判定 |
| E3 | 任意属性值模式 | `Pattern(ANY_SPECIFIC)`（op 级修饰） | 静态产物（见下） | "等于某个具体（非空/非缺省）值，值未知"——值模式，不是存在性 |
| E4 | 未知观测值 | `value_kind=UNKNOWN`（§2.2） | 观测侧 | 观测缺失/失败/不可比 |
| E5 | 显式存在性谓词 | `op=EXISTS`（PredicateOp 既有） | 谓词侧 | 断言实体/值存在 |

**E3 ≠ E5**：`Order.status = ANY_SPECIFIC` 不蕴含 `EXISTS(Order.status)` 的反向（EXISTS 对 MISSING 判 FALSE、NULL 判 TRUE；ANY_SPECIFIC 对 NULL/MISSING 均不匹配——"等于某个具体值"排除 NULL）。

**旧 SSTG 串中 `*` 的有类型解析**（映射器输入约定，按**边上下文**消歧；无法判定 → PARTIAL/UNMAPPABLE，禁止猜测）：

| 来源上下文 | 例 | 解析 | 状态 |
|---|---|---|---|
| produces 边（builder `:70` value=None→`*`） | `db.orders.owner=*` | "写某值"→ 后置模式 `Pattern(ANY_SPECIFIC)`（生产侧语义：执行后该字段有某具体值） | MAPPED（值盲注记） |
| requires 原子（goal_to_plan `_goal_atom:325` 动态 EQ → `X=*`） | `db.orders.owner=*` | "等于某具体动态值"→ 前置模式 `Pattern(ANY_SPECIFIC)` | PARTIAL（静态无法给出 expected，判定需运行时绑定具体值后进行；绑定前 eval=UNKNOWN） |
| `Session.authed_as=<authed>`（builder 哨兵） | — | `TRUTHY(Session.authed_as)` | MAPPED |
| `entity` 位 `*`（StateKey 缺省） | — | E1 全局 | MAPPED |
| 裸 `*` 无上下文 / 值域外记号 | `X=*` 且无边信息 | 不解析 | UNMAPPABLE(cause="star-without-context") |

〔当前行为〕`builder.py:70` 与 `goal_to_plan.py:325` 的 `*` 产自两处不同代码、两种语义，消费侧（backward/SSTG 匹配）目前只做字符串相等——E3 两义在字符串层不可分。〔拟议契约〕映射器按边 kind（produces/requires）+ 产源标注消歧；SSTG 串本身**不改**（兼容输入），类型化解析结果进映射产物。〔迁移成本〕映射器实现时需读 SSTG 边属性（`d["kind"]`），不需要改 builder 输出。

---

## 4. 证据等级：来源与使用边界（R4 强化）

四级定义延续 v0（STATIC_INFERRED / CONTROL_FLOW_PROXY / RUNTIME_VERIFIED / UNKNOWN，不赋线性排序），**追加两条硬规则**：

**R4-a 代理独立性**：`CONTROL_FLOW_PROXY` 证据**只能**由独立的控制流/覆盖分析产生（守卫真分支行覆盖、写点锚点覆盖——即 `reached_state_atoms` 机制），**不得由观测失败/UNKNOWN 降级转化产生**。观测失败的产物是 UNKNOWN 事件 + 失败计数（§8.3），不是代理证据。

**R4-b 关联不合并**：同一目标上 proxy 判达与 verified 事实可同时存在。允许**关联呈现**（如报告行 `atom=X, proxy=true, verified=false, correlated=true`），但两来源分桶存储、分键渲染（`state_ledger.json` 双键纪律延续），置信边界随桶携带（proxy 行带 `via="coverage-anchor"`；verified 行带 `via="observer:db"`）。

---

## 5. 映射完整性与可观测性：两个独立维度（R6）

〔当前行为〕v0 草案把"靶无观测器"混入 UNMAPPABLE 风险；实际 `Session.authed_as=<authed>` 语义完全可解析，只是 vulnshop 无 session 真源（`state_evidence.py:21-24` 自认）。

〔拟议契约〕拆为正交两维：

```text
维度一 MappingStatus(语义映射完整性,适配器输出):
  MAPPED      原子 → 完整 (subject, op, expected, entity_kind)
  PARTIAL     可解析但缺判定要素(如 ANY_SPECIFIC 模式缺具体值;实体变量待绑定)
  UNMAPPABLE  语义解析失败(cause: star-without-context / unknown-sentinel / alias-miss …)

维度二 ObservabilityStatus(运行时可观测性,由观测器能力登记表输出,与映射无关):
  OBSERVABLE       观测器声明该 subject 主体族有真源(vulnshop: Order.*/User.*/Coupon.*)
  NOT_OBSERVABLE   观测器显式声明无真源(vulnshop: Session.*——Bearer 无状态)
  UNPROBED         无观测器 / 未启用 --observe-state(整体未知)
```

组合语义（九格中的关键格）：

| 组合 | 例 | 覆盖指标处理 |
|---|---|---|
| MAPPED ∧ OBSERVABLE | `Order.status=PAID`@vulnshop | verified 判定可做；进 §9 静态目标覆盖分母 |
| MAPPED ∧ NOT_OBSERVABLE | `Session.authed_as=<authed>`@vulnshop | 语义目标成立但运行时不可验证；**不进** verified 分母，进不可观测目标计数；其 proxy 判达（若有）另桶 |
| MAPPED ∧ UNPROBED | 任意靶 + 未开观测 | 同上（UNPROBED 整体未知）；缺块≠零观测语义延续 |
| PARTIAL ∧ * | `X=*`(requires) | 可入分母但必须带 PARTIAL 标注且判定仅当绑定完成（§6）；未绑定即 UNKNOWN |
| UNMAPPABLE ∧ * | 无上下文 `*` | 不入任何覆盖分母；入映射质量指标（cause 分布） |

适配器接口（签名草案，Phase 1 T5 实现）：

```python
def adapt_atom(atom: str, *, edge_kind: str | None,
               aliases: "AliasTable") -> "MappedAtom"

@dataclass(frozen=True)
class MappedAtom:
    status: MappingStatus            # MAPPED/PARTIAL/UNMAPPABLE
    subject: str | None              # "Order.status"
    op: "PredicateOp | None"         # EQ/EXISTS/TRUTHY/...
    expected: object | None          # EQ 值;模式时 None+pattern 标记
    pattern: str | None              # "ANY_SPECIFIC"(E3)或 None
    entity_kind: str                 # resource/user/session/global
    needs_binding: bool              # True → 判定前须过绑定环境(§6)
    cause: str | None                # UNMAPPABLE/PARTIAL 原因,保真不丢
    notes: tuple[str, ...]
```

观测器能力登记（靶侧声明，非推断）：`RuntimeObserver` 协议增加可选属性 `subject_families -> dict[str, ObservabilityStatus]`（缺省 {} = UNPROBED 全体）——vulnshop 实现时声明三族 OBSERVABLE + Session 族 NOT_OBSERVABLE。〔迁移成本〕协议属性追加 + vulnshop 观测器 +4 行；`NullObserver` 不变（零事实）。

---

## 6. 联合实体绑定与目标判达（R3）

### 6.1 绑定环境

```python
@dataclass
class BindingEnv:
    """一次目标判达尝试的共享实体绑定环境。"""
    candidates: dict[str, set[str]]        # "?o" -> {"order:3", "order:4"}
    provenance: dict[tuple[str, str], str] # ("?o","order:3") -> "runtime-observe"|"symbol-slot:?oid"|"request-param"
    joins: tuple["JoinConstraint", ...] = ()   # 显式跨主体关联约束(6.2,评审轮新增)

    def add(self, var, entity, source) -> None: ...      # 多候选并存
    def conflict(self, var) -> bool: ...                 # 强制槽与观测不一致(6.4)

@dataclass(frozen=True)
class JoinConstraint:
    """跨主体关联:两原子(可属不同主体族)的实体/取值必须满足的关系。"""
    left: str          # 原子引用 "Order.owner(?o)"
    right: str         # 原子引用 "User.username(?u)"
    relation: str      # "entity-eq" | "value-eq" | "value-in"(词表可扩,Phase 3 冻结)
```

**〔v1.1b 裁决〕`value-eq` 由关系原子（§6.6）覆盖**：`Equal(Field(?o,"owner"), Field(?u,"username"))` 本身就是值相等关系——目标含关系原子时**不得**再叠加同义的 value-eq join（双轨冗余）。`JoinConstraint` 词表收窄为承载关系原子表达不了的两种形态：`entity-eq`（两变量绑同一实体实例）与 `value-in`（集合成员关联）；求值器对关系原子与收窄后的 join 一视同仁。

**候选绑定来源**（优先级从高到低，冲突时按 6.4 裁决）：
1. **运行时观测枚举**：观测器产出的全部实体实例（如 orders 表全部行 id）——`OBSERVABLE` 主体族的权威来源；
2. **符号槽回填**：规划链 `extract` 写回的 `?oid` 等槽值（vulnshop `_BINDINGS` 机制）；
3. **请求参数**：路径/JSON 中的 id 形参数值（弱证据：发出请求 ≠ 实体存在）。

### 6.2 静态目标 → 待绑定变量的生成规则

静态 SSTG 原子无实体维度。映射时按 `entity_kind` 生成变量：

- `entity_kind ∈ {resource, user}`：主体族带实体 → **缺省推导规则**：每主体族一个变量（`Order.*` 原子共享 `?order`；`User.*` 原子共享 `?user`）。
- `entity_kind ∈ {session, global}`：E1 全局，无变量（直接判定）。

**〔评审轮新增〕缺省规则 ≠ 永久关联规则**：族内共享变量是映射期的**缺省推导启发**（Phase 1 只实现它），不得固化为判定器的硬编码——跨主体关联必须经 `JoinConstraint` **显式声明**并在求值时检查。缺省推导不生成跨族 join；需要跨族约束时（如 `Order.owner(?o)` 必须等于某 `User.username(?u)`）由目标构造方（Phase 3 前沿对象 / goal 模板）显式给出 join，求值器对缺省与显式 join 一视同仁。

```text
goal G1 = { Order.owner(?order) == user:A , Order.status(?order) == PAID }
         (User 侧 user:A 为具体常量,不设变量;若目标同时含 User.role(?user)==admin
          且另有 User.id(?user)==A 的锚定原子,则 ?user 绑 A——由候选枚举+判定联立)
跨族例 G2 = { Order.owner(?order) , User.username(?user) , User.role(?user)==admin }
         + Join(Order.owner(?order), User.username(?user), "value-eq")
         ⇒ "某订单属于某 admin 用户"——族共享推导不出此关联,join 显式承载。
```

### 6.3 判定语义

```python
def evaluate_goal(atoms: list[MappedAtom], env: BindingEnv,
                  facts: "ObservationView") -> "GoalEvaluation"

@dataclass(frozen=True)
class GoalEvaluation:
    status: "ACHIEVED | NOT_ACHIEVED | UNKNOWN"
    satisfying: tuple[dict[str, str], ...]   # 满足的绑定赋值(可多个;存在量词语义)
    per_atom: dict[str, str]                 # 原子 → TRUE/FALSE/UNKNOWN(按最优赋值)
    reason: str                              # 可读原因(无一致绑定/候选耗尽/观测缺失…)
```

- **多原子联合判达**：`status=ACHIEVED` 当且仅当 **存在一个赋值 σ 使全部原子 eval=TRUE**。不同实体分别满足不同原子（order3.owner=A + order4.status=PAID）→ `NOT_ACHIEVED, reason="no-consistent-binding"`。
- **单原子存在性命中**（退化情形）：目标只含一个带 `?x` 原子 → 存在量词，任一实体满足即 ACHIEVED（§9 静态目标覆盖的判定粒度；实例新颖度另行按实体计数，两指标分开正是为此）。
- **多候选**：candidates 逐个尝试（绑定枚举上限可配，防实例爆炸；超限 → UNKNOWN+reason="candidate-cap"）。
- **候选为空**：`?o` 无任何候选 → `UNKNOWN`（不是 NOT_ACHIEVED——没有实体不等于没有可满足赋值的证明；除非 EXISTS 类锚定原子判 FALSE）。
- **绑定冲突**（6.4）：强制槽（符号槽指定 ?o=3）与运行时观测（owner 判定只对 order:4 成立）不一致 → 记 `Conflict(intended=3, viable=4, sources=[...])`；裁决：**运行时观测优先**，槽值降级为 hint；冲突入审计（不静默改写）。

### 6.4 与既有机制的关系

符号槽 `?oid`（BindingContext/vulnshop `_BINDINGS`）是**规划执行层**绑定；本节 BindingEnv 是**判达评估层**绑定——Phase 3 前沿判达执行时，规划链的槽回填作为候选来源 2 注入 BindingEnv（复用，不替代）。〔当前行为〕无任何联合判达实现；`reached_atoms` 是原子级独立判定（无绑定概念）。〔迁移成本〕evaluate_goal 为新纯函数（state 域），消费者 Phase 2/3 接线；SSTG/backward 不改。

### 6.5 判达状态记账：AchievementStatus（评审轮新增）

`evaluate_goal` 的 `ACHIEVED/NOT_ACHIEVED/UNKNOWN` 是**状态谓词求值**；规划/前沿循环消费的是**判达记账状态**——后者必须把可观测性维度（§5）与代理证据（§4）合并进来，且 **UNVERIFIABLE 不得视为规划失败**：

```text
AchievementStatus ∈ { VERIFIED_ACHIEVED, VERIFIED_NOT_ACHIEVED,
                      PROXY_PROGRESS, UNVERIFIABLE, UNKNOWN }
```

推导表（`classify_achievement(goal_eval, observability, proxy_evidence)`）：

| observability | goal_eval（§6.3） | proxy 证据 | → AchievementStatus | 前沿策略 |
|---|---|---|---|---|
| OBSERVABLE | ACHIEVED（verified 事实支撑） | 任意 | **VERIFIED_ACHIEVED** | 目标关闭；成功链回注 |
| OBSERVABLE | NOT_ACHIEVED（全候选有 FALSE） | 任意 | **VERIFIED_NOT_ACHIEVED** | 记失败前置条件；按签名防重复同链 |
| OBSERVABLE | UNKNOWN（观测失败/候选不足） | 任意 | **UNKNOWN**（瞬态） | 重试观测；不计失败 |
| NOT_OBSERVABLE / UNPROBED | 不可求值 | 有（如登录写锚覆盖） | **PROXY_PROGRESS** | 继续推进；永不升 VERIFIED；目标标记"仅代理可证" |
| NOT_OBSERVABLE / UNPROBED | 不可求值 | 无 | **UNVERIFIABLE** | **不视为失败**；标记目标不可验证，防反复规划已可能成功的链 |

**关键规则**：
1. `UNVERIFIABLE` 描述**能力缺失**（无独立状态观测源），不是负向证据——禁止进失败统计、禁止触发"目标不可达→换目标/重规划"路径。典型例：vulnshop 的 `Session.authed_as` 目标链（登录链可能已执行成功，但该靶无 session 真源，永远无法 verified——若按失败处理，前沿会对同一登录链无限重规划）。
2. `PROXY_PROGRESS` 是 NOT_OBSERVABLE 目标的**最高可得记账**：可以推进前沿（继续依赖链），但永不与 VERIFIED_ACHIEVED 混计（§9.1 分母规则延续）。
3. `VERIFIED_NOT_ACHIEVED` 需要可靠观测（全候选赋值下有 verified FALSE）——与"没观测到达成"（UNKNOWN）严格区分；后者不构成反证。
4. 状态迁移允许：UNKNOWN →（观测成功后）→ 任意终态；UNVERIFIABLE →（观测器能力变更，如换靶/加观测器）→ 重新进入推导。终态对同一 (goal, binding) 幂等。

〔当前行为〕无此记账；`reached_atoms` 布尔口径会把 NOT_OBSERVABLE 目标永远报"未达"。〔拟议契约〕Phase 3 前沿对象消费本表；§9.1 指标的分子=VERIFIED_ACHIEVED 目标集，PROXY_PROGRESS/UNVERIFIABLE 各自单列。〔迁移成本〕`classify_achievement` 为纯函数（与 evaluate_goal 同文件），测试先行；前沿对象 Phase 3 落地。

### 6.6 关系约束目标：RelationalGoal（评审轮二新增，v1.1b）

**动机（事实核查，2026-10-18）**：`vulnshop/app.py` `ship_order` 的守卫 `if order["user_id"] != user["id"]: return 403` 与 `if order["status"] != "PAID": return 409`，经 `extract_state_facts` 实测**零事实产出**（现有 `_extract_db` 守卫分支只认 `obj.attr` 形态；`_extract_dict_store` 只认 `_DB` 容器别名——SQL 行变量 `order`/`user` 两边都不匹配）。即：单主体 `StructuredPredicate` 词表**表达不了**双动态下标比较，这正是"订单归属"这类关系语义丢失的根因。最小原型（early-return NotEq 反转 + `ast.unparse`）在真实 `ship_order` 上已复验可提取 `order['user_id']==user['id']`（行 271）与 `order['status']=='PAID'`（行 273）——可行性有据。

**设计原则（评审裁定采纳）**：不让系统猜业务语义；让它追踪**变量从哪里来（provenance）、在哪里比较（relational constraints）、被谁修改、对应哪个运行时对象（binding）**。语义关系必须有源码位置、控制流路径、变量来源三重依据，不由 LLM 编造。

#### 6.6.1 目标表示：Term 与关系谓词

```python
@dataclass(frozen=True)
class Field:                 # 属性引用:实体变量 + 字段(变量身份,非裸字符串)
    var: str                 # "?order" / "?user"(E2 未绑定实体变量)
    name: str                # "user_id" / "id" / "status"
@dataclass(frozen=True)
class Const:
    value: object            # "PAID" / 1 / None

RelPredicate := (op ∈ {EQ, NE, IN, NOT_IN, TRUTHY, FALSY},
                 left: Field, right: Field | Const)
```

- 旧单主体谓词是**退化特例**：`(Order.status, ?o, EQ, "PAID")` = `EQ(Field(?o,"status"), Const("PAID"))`。`StructuredPredicate` 保留为兼容输入形态，映射到 RelPredicate（§5 适配器扩展），不并行两套。
- SSTG 原子（`Var=value`）同为退化特例（Field vs Const，实体维度缺省 `?o`）——SSTG **不改**，仍作规划动作链的词表（backward 照常消费）；RelationalGoal 作**判达与验证的目标表示**。两轨分工：SSTG 管"怎么构造"，RelationalGoal 管"构造对了没有"。

#### 6.6.2 变量身份与数据来源追踪（Provenance）

`Field.var` 不是自由名，必须携带来源链（证明 `?order` 确是"按 oid 查询的订单行"而非任意字典）：

```text
?order ← get_order_or_none(oid) ← route 参数 oid(POST /orders/<int:oid>/ship)
         └ 查询体: SELECT * FROM orders WHERE id=? (参数化 → 行实体 = orders[id=oid])
?user  ← require_user() ← Authorization 头 token → 当前请求认证主体
provenance 链 = (源码锚点, 中间函数, 终端容器[表/会话/请求])
```

需要的三种分析能力（复用既有件，不新建框架）：① 局部数据流（`order = get_order_or_none(oid)` 的赋值追踪）；② 跨函数展开（查询函数体的 SQL/ORM 读取面——`_extract_db` 已解析 `SELECT * FROM orders WHERE id=?`，缺的是"返回值 → 调用者变量"的绑定）；③ 守卫方向（early-return `if A != B: return` ⇒ 继续 requires `A == B`——term 级推广 goal_derivation 既有方向感知反转，机制同源）。

诚实边界（MVP）：SQL 行变量（`order["k"]`，vulnshop 形态）与 dict-store 行别名（`_DB` 容器，微靶形态，`_row_alias_map` 既有）先做；ORM 属性形态（`obj.attr`，`_extract_db` 既有）并入；`can_refund(user, order)` 这类**封装谓词**按 §6.6.3 三级策略处理，不强行内联展开。

#### 6.6.3 语义发现的三级机制与 LLM 边界

| 级 | 机制 | 可发现 | 可靠度 | 产物证据等级 |
|---|---|---|---|---|
| ① 静态 | 数据流 + 条件比较 + SQL/ORM 分析 | 字段相等、资源绑定、状态守卫、明确写入 | 来源链完整时高 | STATIC_INFERRED（附 provenance 锚点） |
| ② 动态 | 执行 + 只读状态观测 | 真实创建 ID、状态迁移、关系是否实际成立 | 有观测源时高 | RUNTIME_VERIFIED |
| ③ LLM | 辅助推理（封装函数/间接依赖/非标准命名） | **候选关系** | 仅候选 | STATIC_INFERRED 子标签 `llm-proposed` |

硬规则：③ 的产物必须经 ② 验证或保持 PARTIAL/UNKNOWN，**永不自动升级**；①②③ 任何一级不确定时**明确停止推断**（UNKNOWN），系统目标是"逐步收集可靠关系"而非"完全理解程序"。

#### 6.6.4 运行时绑定与联合验证

- 绑定：复用 BindingContext/`extract` 符号槽回填（请求响应的 `{"id": 12}` → `?order=order:12`）+ §6.1 候选来源；**绑定必须记录来源**（请求声称 ≠ 服务器认定——token 请求的认证主体以观测为准）。
- 求值：`resolve(term, snapshot, bindings)`（Const→值；Field→绑定的实体行字段，未绑定/行缺失→UNKNOWN）+ `evaluate(relgoal, snapshot, bindings)` → TRUE / FALSE / UNKNOWN（任一 term UNKNOWN 且无 FALSE → UNKNOWN，不虚构）。
- **同一观测点约束**：snapshot 必须来自**单次一致观测**（一次 `observe()` 批次），禁止跨请求/跨时间拼接历史事实求值（历史台账只作 at-least-once 覆盖证据，§2.1）。
- 观测器字段义务：验证关系所需的字段必须由观测器产出。〔事实〕vulnshop 观测器现只输出 `Order.exists/Order.status`（`SELECT id, status`），**缺 `Order.user_id`**——归属关系验证的前提补齐项（INTEGRATION_PLAN T2 增补）。

#### 6.6.5 与既有契约条款的关系

- §6.1：JoinConstraint 词表收窄（value-eq 归关系原子，见 §6.1 v1.1b 裁决）；
- §6.3：`evaluate_goal` 的原子列表泛化为 RelPredicate 列表（联合判达/同一 σ 语义不变——`Equal(Field(?o,"user_id"), Field(?u,"id"))` 天然以同一赋值求值，例 2 反例由此结构性排除）；
- §5：适配器增一路输入——守卫关系提取器（§6.6.2）直接产 RelationalGoal（非退化形态）；SSTG 原子路径（退化形态）不变；
- §4：三级机制的证据等级映射入既有四级（无新等级）。

---

## 7. UNKNOWN 语义（v0 §5 延续，修订两点）

v0 六条保留（三值布尔、缺失实体族 UNKNOWN、枚举值域静态∪运行时、不可观测显式登记、通配不与具体值判等、渲染串≠语义），修订：

1. **缺失实体细化**（对接 §2.2）：v0 的"枚举失败 → 谓词族整体 UNKNOWN"细化为：实体**枚举失败**（读表异常）→ 族 UNKNOWN + 观测失败计数；实体**枚举成功但不含某实体**（行不存在）→ 该实体 MISSING，仅 EXISTS 可判 FALSE（§2.1 表）。
2. **UNKNOWN 判定传播**：多原子联合判定中，任一原子 UNKNOWN 且无其他赋值可绕过 → 整体 UNKNOWN（不是 NOT_ACHIEVED）；仅当**全部候选赋值下均有原子判 FALSE** 才是 NOT_ACHIEVED。

---

## 8. 迁移契约（R4 修订）

### 8.1 verified 迁移定义（双端可靠）

```text
StateTransition := (from_fact, to_fact, trigger, request_span, evidence_pair, dedup_key)
  产生条件(全部满足,缺一不可):
    a. before/after 两次观测都成功(观测器正常返回,无异常);
    b. from_fact/to_fact 均为 RUNTIME_VERIFIED(§2.1 观测事实),同 (subject, entity);
    c. 值变化真实(from≠to,按 (subject,entity,observed_value,value_kind) 比较);
    d. 请求标签与 request_span 完整(input_id, request_index, before_at, after_at)。
  dedup_key = (from.key+value, to.key+value, trigger)   # 与 StateRegistry.add_edge 同形
```

- 枚举实体缺失（before 有 order:3、after 实体集无 order:3）→ 不连线，显式记 `entity-vanished(order:3)` 事件。
- 会话边界 `new_session()` 重置语义保留；跨会话不连线。

### 8.2 观测失败与代理的隔离（R4 硬规则落地）

- before 或 after 任一**失败/缺失/UNKNOWN** → **不产 verified 迁移**；记 `TransitionAttempt(status=unknown, cause=...)` 事件（原因保真：observer-error / entity-enumeration-failed / unobservable）。
- **不得**由该失败自动补一条 CONTROL_FLOW_PROXY 迁移（R4-a）。proxy 迁移（若独立通道存在：守卫/写点覆盖分析）单独产生、单独存储；与 verified 迁移可 `correlated` 呈现（R4-b），各自带来源与置信边界。
- 中间瞬态（before≠after 之间发生的不可见变化）不臆测：迁移只声明两端，不声明路径唯一性。

### 8.3 请求异常/HTTP 错误但有副作用时的顺序（R4 新增）

```text
单请求 R_i 执行(含异常路径):
  before_i = observe()                # untraced 窗口(§10)
  try: execute(R_i)
      except Exception as e: error_i = e      # 记录,不吞(拟议;现状被 collectors 吞掉)
  after_i  = observe()                # ★ 无论 error_i 与否都观测——副作用可能已发生
  #   HTTP 4xx/5xx 同理:响应错误 ≠ 无副作用(部分支付/部分写)
  lifecycle: 序列末尾 end_run 还原    # 现状顺序保留;还原失败 fail-closed 沿用
  outcome_i = (before_i, after_i, error_i)    # error_i 非 None → 迁移仍可产生(8.1 条件
  #   只看观测可靠性),但事件带 error 标注,审计可筛"错误请求引发的迁移"
```

〔当前行为〕`candidates.py:71-74` 吞掉 target 异常且不记录；HTTP 状态码在 fuzz 面不可见（`ExecutionResult.response` 恒 None 兼容层，`execution_context.py:210-213`）。〔拟议契约〕请求级 error 记录（演进键）；观测/还原顺序如上，不因错误跳过 after 观测。〔迁移成本〕collector 加错误捕获字段（Phase 2 逐请求观测一并做）；off/legacy 路径行为不变。

---

## 9. 覆盖率与新颖度指标：四类分离（R5）

**禁止**实例级计数除以静态目标分母。四类指标各走各的口径：

### 9.1 静态目标覆盖率（goal 粒度，有界 [0,1]）

```text
StaticGoalCoverage = |{g ∈ G_observable : achieved_at_least_once(g)}| / |G_observable|
  G_observable = {静态目标 g : mapping(g)=MAPPED ∧ observability(g)=OBSERVABLE
                  ∧ g 的绑定约束在历史观测下可评估}
  achieved(g) = ∃σ∈BindingEnv 候选赋值: ∀a∈atoms(g): eval(a, facts, σ)=TRUE
                (历史语义:verified 台账 at-least-once;多原子须同一 σ——§6.3)
处理规则:
  - PARTIAL 目标:仅当历史绑定使其可判定时计入分母并参与判定,否则排除并单列
    partial_pending_binding(不算未覆盖——语义上"未判定");
  - NOT_OBSERVABLE / UNPROBED / UNMAPPABLE 目标:不入分母,单列计数
    (unobservable_goals / unmappable_goals),映射质量与可观测性独立报告;
  - 每目标计一次 ⇒ 分子 ≤ 分母恒成立(结构性上限 100%)。
```

**反例验证**（任务书例 7）：10 个订单实例都 verified 为 `Order.status=PAID` → 目标 `Order.status=PAID` 的 achieved 计 **1**；G_observable 若只有这一个目标 → 1/1=100%，**绝不会 10/1**。实例层面的收获体现在 9.2。

### 9.2 运行时实例状态新颖度（实例粒度，计数/曲线，无静态分母）

```text
InstanceStateNovelty = |首见 (subject, entity, observed_value, value_kind) 元组|
  报告形态:计数与增长曲线(随 epoch/时间),不与任何静态原子数相除。
```

### 9.3 运行时 verified 迁移新颖度（迁移粒度）

```text
VerifiedTransitionNovelty = |首见 StateTransition(§8.1 全条件)数|
  若需报告"覆盖率",分母必须同口径可定义:双端可观测(OBSERVABLE ∧ 观测成功)
  的静态迁移候选闭包;分母不可定义时只报计数(缺省推荐:只报计数)。
```

〔当前行为〕`state_arm_report.py:150-157` 的 `transitions_observed` 是**静态 SSTG 边 × 代理判达原子**的派生量（produces/requires 边 where 原子 ∈ reached），非运行时观测迁移。〔拟议契约〕该量更名标注 `proxy_derived_transitions`（旧键 `transitions_observed` 保留兼容，双写过渡），与 9.3 分桶。〔迁移成本〕报告层加新键不改旧键。

### 9.4 代理进展（延续现状口径）

`reached_state_atoms`（覆盖代理）计数与增长延续 `state_ledger.json` 双键纪律（`proxy_reached_atoms` / `verified_reached_atoms`），**永不**与 9.1–9.3 合计或相除。

---

## 10. 逐请求观测时点契约（v0 §7 延续 + 错误路径）

v0 的序列采样骨架保留（begin_input → 逐请求 before/after → finish_input 还原），并入 §8.3 错误顺序与观测失败计数（按输入/按请求分别）。观测成本计入 feedback_seconds 并细分（C9 演进键 `observe_seconds`）。无观测器靶：全部 UNKNOWN，不虚构。

---

## 11. 正反例（八类，任务书第三节）

以下为**可执行语义示例**：输入事实/目标 → 期望判定 + 原因。实现时固化为表驱动测试（见 INTEGRATION_PLAN 测试计划）。

**例 1（正例：同一订单的观测值序列与 verified 迁移）**
```text
facts（观测事实序列,§8 成对采样）:
  req1.before: (Order.status, order:3, ENUM, "PENDING")
  req1.after:  (Order.status, order:3, ENUM, "PAID")       ← req1 = 支付请求
  req2.before: (Order.status, order:3, ENUM, "PAID")       （链上 = req1.after,一致）
  req2.after:  (Order.status, order:3, ENUM, "CANCELLED")  ← req2 = 取消请求
transitions（§8.1,每条 = 同一请求的 before/after 对）:
  T1 = (PENDING → PAID,     trigger="pay",    span=(input#1, req1))
  T2 = (PAID → CANCELLED,   trigger="cancel", span=(input#2, req1))
期望: 2 条 verified 迁移;台账历史含 PENDING/PAID/CANCELLED 三个值事实(at-least-once,§2.1);
       req2 之后的当点判定 eval(EQ"PAID") = FALSE(值已变,历史≠当点)。
```

**例 2（反例：不同订单错误拼接）**
```text
goal G = { Order.owner(?o)==user:A , Order.status(?o)==PAID }
facts:  (Order.owner,  order:3, "A")   (Order.status, order:3, "PENDING")
        (Order.owner,  order:4, "B")   (Order.status, order:4, "PAID")
期望: NOT_ACHIEVED, reason="no-consistent-binding"
       (order3 owner 真但 status≠PAID;order4 反之;无同一 ?o 全真——§6.3)
```

**例 3（UNKNOWN ≠ FALSE：观测失败/实体枚举失败）**
```text
情形a: observe() 抛异常(read failed) 于 orders 族 → Order.* 全部无观测
       → eval(EQ"SHIPPED", order:3) = UNKNOWN(≠FALSE);计 observer_failures+1
情形b: 实体枚举成功,orders 无 id=9 行 → (Order.*, order:9) MISSING
       → eval(EXISTS, order:9) = FALSE(唯一 MISSING→FALSE 通道,§2.1)
       → eval(EQ"PAID",  order:9) = UNKNOWN(不是 FALSE)
```

**例 4（未绑定变量/多候选/绑定冲突）**
```text
a) ?order 候选为空(无观测) → UNKNOWN, reason="no-candidates"(非 NOT_ACHIEVED)
b) 候选 {3,4}:3 上 owner 真/status UNKNOWN,4 上 owner FALSE/status 真
   → 存在赋值使 owner 真(σ={?o:3})但该赋值下 status UNKNOWN → 整体 UNKNOWN
   (须全 TRUE 才 ACHIEVED;须全候选有 FALSE 才 NOT_ACHIEVED——§7.2)
c) 冲突:符号槽 ?oid 回填 3,但运行时观测仅 order:4 满足目标原子
   → Conflict(intended=3, viable=4) 入审计;运行时观测优先,槽降级 hint(§6.4)
```

**例 5（`*` 通配值 ≠ EXISTS）**
```text
SSTG produces "db.orders.owner=*" → Pattern(ANY_SPECIFIC)(§3-E3)
facts: (Order.owner, order:5, NULL) → eval(ANY_SPECIFIC) = 不匹配(NULL 排除)
       eval(EXISTS, order:5)        = TRUE(NULL 也是有值——§2.1 EXISTS 行)
⇒ 两谓词在 NULL 上判定相反,不得视为同一目标。
```

**例 6（观测失败不产代理迁移）**
```text
req5: before 观测成功(PAID),after 观测失败(observer-error)
期望: 无 verified 迁移(§8.2);记 TransitionAttempt(status=unknown,cause=observer-error)
      不得自动记 proxy 迁移;若独立覆盖分析恰显示写点锚点已覆盖,
      proxy 判达单独存在,报告行: atom=X, proxy=true(via=coverage-anchor),
      verified=false, correlated=true —— 两来源分存(§4 R4-a/b)。
```

**例 7（覆盖率上限）**
```text
G_observable={Order.status=PAID};10 个订单实体均 verified PAID
期望: StaticGoalCoverage = 1/1 = 100%(achieved 计 1 次)
      InstanceStateNovelty += 10(首见元组)
      绝无 10/1=1000%(结构性:目标计一次,§9.1)。
```

**例 8（可映射但缺运行时观测器的 Session 状态）**
```text
atom "Session.authed_as=<authed>" @vulnshop
映射: MAPPED(op=TRUTHY, subject=Session.authed_as, entity_kind=session)
观测: NOT_OBSERVABLE(state_evidence.py 自认无 session 真源)
期望: 不进 G_observable(9.1 分母);进 unobservable_goals 计数;
      其覆盖代理判达(若有)在 proxy 桶另计;语义目标本身仍可用于规划
      (SSTG requires/plan_backward 照常消费,不受观测维度影响——§5 两维正交)。
```

**例 9（评审轮补充：跨主体关联必须显式 join，族共享推导不出）**
```text
goal G2 = { Order.owner(?order) , User.username(?user), User.role(?user)==admin }
          + Join(Order.owner(?order), User.username(?user), "value-eq")
facts:  (Order.owner, order:7, "bob")  (User.username, user:bob)  (User.role, user:bob, "admin")
        (Order.owner, order:8, "amy")  (User.username, user:amy)  (User.role, user:amy, "user")
期望: σ={?order:7→bob, ?user:bob} 满足 join+role → ACHIEVED(satisfying=[{order:7,bob}]);
      order:8 被 join 排除(owner=amy 非 admin)。
      若缺 JoinConstraint:两族变量彼此独立,role(admin) 与任意订单可拼——
      族共享规则只约束同族同变量,跨族一致性**必须**显式声明(§6.2)。
```

**例 10（评审轮补充：UNVERIFIABLE ≠ 规划失败，防登录链无限重规划）**
```text
靶 vulnshop;goal = Session.authed_as TRUTHY(NOT_OBSERVABLE)
链执行: register→login 成功(login 成功 return 行覆盖 = 写锚 proxy 证据)
记账: classify_achievement(goal_eval=不可求值, obs=NOT_OBSERVABLE,
                           proxy=有) → PROXY_PROGRESS(非 VERIFIED,非失败)
      前沿策略: 该目标标记"仅代理可证",链标记已推进,**不再因"未 verified"
      重规划同一登录链;若 proxy 亦无 → UNVERIFIABLE,同样不进失败统计(§6.5)。
反例(禁止): 把 UNVERIFIABLE 当失败 → 前沿反复选同一目标 →
      plan_backward 产出同一链(内容哈希 DUPLICATE) → 无限循环。
```

**例 11（评审轮二补充：ship_order 关系目标端到端，M0 验收蓝本）**
```text
靶 vulnshop(guards=True),目标块 = UPDATE orders SET status='SHIPPED'
静态提取(§6.6.2,无手写归属规则):
  R1 = EQ(Field(?order,"user_id"), Field(?user,"id"))   ← 行 271 反转
  R2 = EQ(Field(?order,"status"),  Const("PAID"))        ← 行 273 反转
  provenance: ?order←get_order_or_none(oid)←route oid;?user←require_user()←token
执行链: register→login(?user)→POST /orders(?order:12)→pay→ship
  绑定(带来源): {?user: user:7(slot:token), ?order: order:12(slot:id)}
单点快照(一次 observe): user:7{id:7,...} order:12{user_id:7,status:"PAID"}
判定: resolve(R1)=7==7 TRUE;R2 PAID==PAID TRUE → ACHIEVED(σ 同一赋值)
反例a(错误归属): order:12.user_id=8 ≠ user:7.id → FALSE(VERIFIED_NOT_ACHIEVED 路径)
反例b(跨单拼接): owner 用 order:7、status 用 order:12 → 无同一 ?order 全真
        → NOT_ACHIEVED(no-consistent-binding,§6.3)
反例c(观测缺字段): 快照无 user_id 列(观测器未输出) → UNKNOWN,禁止判 TRUE/FALSE
        —— 即 §6.6.4 观测器字段义务(Order.user_id 必须补)的存在理由。
```

---

## 12. 落地对照表（当前行为 / 拟议契约 / 迁移成本）

| 条款 | 当前行为 | 拟议契约 | 迁移成本 |
|---|---|---|---|
| §2 三分模型 | 观测值嵌谓词串;无判定函数 | 观测/谓词/判定三分 + v2 可选列 | 低:观测器补列,ledger 透传 |
| §2.2 值类型 | 无显式区分(NULL/MISSING/UNKNOWN 隐式) | ValueKind 七值显式 | 中:观测器与判定函数 |
| §3 `*` 五义 | 字符串层混用(builder/goal_to_plan 两源同形) | 专 token + 边上下文有类型解析 | 低:映射器读边 kind,builder 不改 |
| §4 代理独立性 | proxy 由覆盖分析产生(符合);失败降级路径不存在但也无明文禁止 | 明文 R4-a/b 硬规则 | 零(立法性条款)+ 测试钉死 |
| §5 两维正交 | v0 草案混淆;现观测器事实分离但无登记表 | MappingStatus × ObservabilityStatus + 观测器族登记 | 低:协议可选属性 + vulnshop 声明 |
| §6 绑定环境 | 无(any-atom 独立判定) | BindingEnv + evaluate_goal 联合判定 + 显式跨主体 JoinConstraint(族共享仅为缺省推导) | 中:新纯函数,Phase 2/3 消费 |
| §6.5 判达记账 | reached_atoms 布尔口径(NOT_OBSERVABLE 永报未达) | AchievementStatus 五值;UNVERIFIABLE≠失败;PROXY_PROGRESS 为不可观测目标上限 | 低(纯函数)+ Phase 3 前沿消费 |
| §6.6 关系约束目标 | extractor 对 `order["user_id"]!=user["id"]` 零产出(实测);单主体谓词表达不了关系;观测器缺 Order.user_id | Field/Const/RelPredicate + provenance 链 + 三级发现(LLM 仅候选)+ 单点快照联合验证;SSTG 规划/RelGoal 判达双轨分工 | 中:extractor 扩展(行变量身份/跨函数展开)+ 观测器补列 + resolve/evaluate;M0 spike 先行 |
| §8 迁移契约 | M2 dynamic:state-diff 仅代理;无 verified 迁移 | 双端可靠 verified 迁移 + 失败事件 | 中:Phase 2 逐请求观测前置 |
| §8.3 错误顺序 | 异常被吞不记录 | error 记录 + after 观测不跳过 | 低:collector 字段 |
| §9 指标 | counts(len 并集,state_arm_report)——无比率,无混除风险 | 四类分离公式 + PARTIAL/不可观测处理 | 中:H 系列落地时实现 |
| §10 时点 | 输入级一次 | 逐请求成对 + 错误路径 | 中:Phase 2 主工作 |

*本契约为设计文档;实现以 Phase 1–3 任务推进,冲突处以本文 + CONTRACT_DECISIONS.md 为准,不得反向把实现缺陷写回契约。*

---

## 附录：实现状态对照（2026-10-19，随 PR-00~03 更新）

> 契约条款 → 已实现/未实现。已实现 ≠ 全量语义：范围以对应 PR 测试为准。

| 契约条款 | 状态 | 实现面（测试） |
|---|---|---|
| §2.3 StateObservation v2 四可选列 | ✅ PR-03 | `runtime_observer.py`（`test_observation_v2_optional_columns_roundtrip`） |
| §2.2 ValueKind 七值（NULL/MISSING/UNKNOWN 互斥） | ✅ PR-03 | 同上 + `test_value_kind_vocabulary_and_observability_words` |
| §2.1 判定规则（MISSING→仅 EXISTS FALSE） | ✅ PR-03 | `relational.evaluate_relation` 枚举完整性参数（`test_exists_false_only_with_complete_enumeration`） |
| §6.6.1 关系 IR（Field/Const/RelPredicate） | ✅ 关口 A | `relational.py`（`test_relational.py` 14 项） |
| §6.6.2 provenance（跨函数 SQL/认证/路由/dict-store） | ✅ 关口 A | `provenance.py`（`test_relational_extraction.py`） |
| §6.6.2 词法早退方向 + wrapper 条件化 | ✅ 关口 A | `derive_relational_goal`（同上；guards=1 路径条件） |
| §6.6.4 单点快照联合验证 | ✅ PR-03（子集） | `goal_evaluation.ObservationView/evaluate_goal`（单视图结构约束） |
| §6.3 候选有限枚举 + 同一赋值贯穿 | ✅ PR-03 | `evaluate_goal`（`test_candidate_cap_yields_unknown_not_false`、反例 6） |
| §6.5 AchievementStatus 五值记账 | ❌ 未实现 | `classify_achievement` 仅有签名草案（PR-05 前沿消费时落地） |
| §5 可观测性两维（属性级声明） | ✅ PR-03（子集） | `subject_families`/`observability_of`（精确键无家族回填）；映射适配器 `adapt_atom`（MAPPED/PARTIAL/UNMAPPABLE）未实现（原 T5a） |
| §3 `*` 五义有类型解析 | ❌ 未实现 | SSTG 兼容输入消歧属适配器（T5a）范围 |
| §8 verified 迁移契约（双端成对） | ❌ 未实现 | PR-04（逐请求观测）范围 |
| §9 指标四分离 | ❌ 未实现 | PR-07 范围（`proxy_derived_transitions` 更名也未做） |
| R7 SQLite/WAL 快照 | ✅ PR-02 | `l2_sqlite.py` backup/copy 双模式（`test_l2_wal.py` 10 项） |
| §4 R4-a/b 代理独立性 | 🟡 部分 | 证据分桶现状保持（ledger 只收 verified）；迁移失败不降级的场景测试待 PR-04 |

### §6.6.6 witness 与请求绑定术语(PR-03.1 裁决,2026-10-19)

评审 P0-A/B/D 三漏洞的契约级修复措辞:

- **绑定分类**:`BindingEnv(fixed / enumerable / hints / provenance)`——
  `fixed` = 本次请求确定绑定(路径参数/响应提取/认证上下文),只评估
  所列实例,**不与观测全集并集**;"指定 ?order=12 不得由 order:13 顶替
  出 witness"。`hints` 不参与判定;`provenance` 仅审计展示。
- **认证关联的唯一证据**:同观测点的 verified 探测事实
  `RequestAuth.subject`(observed_value=认证主体实例 id)——
  provenance 字符串("symbol-slot:?token")不构成服务器身份确认。
- **完整判达 = 四分量分类**:`classify_achievement(relations_truth,
  path_conditions_truth, auth_binding_truth, target_trace_covered,
  goal_complete)` → VERIFIED_ACHIEVED / VERIFIED_NOT_ACHIEVED /
  UNKNOWN;witness 只在 VERIFIED_ACHIEVED 产生,携带 path_conditions。
  guards=False 的执行对 guards=True 守卫链目标 → UNKNOWN(不证明也
  不反证);状态 TRUE 而 Trace 未命中 → UNKNOWN(矛盾交审计)。
- 实现面:`state/goal_evaluation.py`(PR-03.1);测试:
  `test_goal_evaluation.py::test_p0a_* / test_p0b_* / test_p0d_*`。
