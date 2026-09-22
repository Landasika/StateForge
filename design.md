# StateForge 静态图设计文档（初步设计 v1）

> 2026-09-21 整理自讨论收敛。上游:`plans/design-v2-state-reachability.md`(v2.1)。
> 核心哲学:**语法挖候选(零白名单)→ LLM 判语义 → 确定性选择器 → 运行时校准**。

---

## 1. 系统输出:三张图

| 图 | 名称 | 节点 | 边 | 回答什么 |
|---|---|---|---|---|
| 图① | **代码控制流图**（G_xfg） | 基本块/函数/端点动作/库叶 | 控制转移+调用+状态虚线 | "代码怎么流、门在哪" |
| 图② | **代码数据流** | 变量定义点/使用点 | def-use 链 | "值从哪来、流到哪去" |
| 图③ | **状态图**（SSTG） | 状态原子（`Order.status=PAID`） | 迁移/produces/requires | "有哪些状态、怎么转移" |

三张图的关系:图①是**代码基底**（状态钉在上面）;④门条件是**从图①推导状态条件的桥梁**;图③是**规划器使用的抽象视图**。

---

## 2. 图① 代码控制流图（G_xfg）的维度

### 2.1 节点类型（7 种）

| 类型 | 含义 | 粒度 |
|---|---|---|
| `action` | 端点动作（`POST /orders/<id>/ship`） | 路由级 |
| `entry` | 函数入口块 | 块级 |
| `block` | 基本块（直线执行，只在入口进入、出口离开） | **块级 ★ 主粒度** |
| `branch` | 分支块（菱形，出 T/F 边） | 块级 |
| `terminal` | 终止块（return，无出边） | 块级 |
| `exit` | 隐式函数出口 | 块级 |
| `ext` | 库函数叶子（sqlite3/flask，不展开） | 函数级 |

**为什么选基本块**：与 Atheris 覆盖反馈计数页、gcov 报告同粒度——代码覆盖率工具的标准单元。

**块的切分规则**：
| 遇到 | 切什么 |
|---|---|
| `if` | 菱形分支块，出 T/F 两条边 |
| `return` | 终止块（无出边） |
| `for`/`while` | 循环头分支块 + 回边 |
| 顺序语句 | 并进当前块（不切） |
| `try` | handler 从 try 入口可达（近似） |

### 2.2 边类型（5 种）

| 边 | 含义 | 方向 |
|---|---|---|
| `cfg` | 控制转移（T/F/回边/异常），`cond` 属性标注分支条件 | 块→块（函数内） |
| `call` | 项目内函数调用 | 调用点所在块→被调函数入口块 |
| `call-ext` | 调库 | 调用点所在块→库函数叶节点 |
| `state-link` | 跨端点共享状态（同变量写锚→读锚） | 端点A写锚块→端点B读锚块 |
| `entry-edge` | 端点动作→处理器函数入口 | action→entry |

### 2.3 锚点（钉在块上的状态事实，4 种）

| 锚 | 含义 | 例子 |
|---|---|---|
| `pre` | 状态前置（比较/真值） | `p:Order.status` = "这里检查了 PAID" |
| `eff` | 状态写（赋值/SQL/构造） | `e:Order.status` = "这里写了 SHIPPED" |
| `guard` | 守卫（owner/role/token） | `g:owner` = "归属检查" |
| `sink` | 危险操作 | `sql_build` = "SQL 拼接点" |

锚点使图①成为**状态增强控制流图**——不是纯代码图，而是在每个代码位置同时知道"这里在检查/写什么状态"。

### 2.4 构建方法（七步）

```
项目源码
   ① 选入口         —— 入口驱动,不扫目录(模拟应用加载方式)
   ② 隔离跑调用图    —— 每个入口单独跑 PyCG,崩者入缺口清单
   ③ 合并去重        —— 并集;入口可达集 = "真·全项目"
   ④ 定库边界        —— 项目函数 vs 库函数(叶子,不展开)
   ⑤ 展开 CFG        —— 项目函数逐个内衬过程内控制流(块级)
   ⑥ 钉锚           —— SA-PR 事实(带 file:line)按行范围钉到块
   ⑦ [待接]运行时校准 —— WATCH 事件给边升/降置信
```

---

## 3. 图② 代码数据流

### 3.1 到达定值（Reaching Definitions）

经典 may 分析：变量的值从哪个定义块传播到哪个使用块。

### 3.2 参数源传播（Parameter Influence）

请求参数（如 `coupon_code`）→ 影响哪个状态锚点（如 `Coupon.used`）。传递路径：参数提取→赋值链→锚点语句。

### 3.3 与图①的关系

数据流在图①的节点（块）上传播——使用图①的控制流边（决定"下一个可能的定义来源"）。

---

## 4. 桥梁：门条件（Block Gates）

**不是数据流，是路径分析**：在图①上做可达性计算，回答"到这个代码块可能要过哪几道状态门"。

算法：对块 b，计算 `反向可达(b) ∩ 正向可达(entry)` 路径上的所有 pre/guard 锚。

语义：**may**（某条路径上可能出现），非 must（必经）。原因：状态门常是条件性的（如 `if GUARDS:` 包住的检查不是必经支配者，但仍是门）。

角色：**从图①推导状态条件的桥梁**——门条件的结果直接成为 SSTG 上 requires 边的来源。

---

## 5. 图③ 状态图（SSTG）

### 5.1 组成

```
LLM 标签(authn/authz/ownership/money/workflow/single_use)
  + 值域(CREATED/PAID/SHIPPED, user/admin, ...)
  + 迁移边(静态 guard→effect 配对)
  + auth 链(LLM relate 的 auth_chains)
  ────────────────────────────────────────
  = SSTG(Security State Transition Graph)
```

### 5.2 节点

| 节点 | 含义 | 例子 |
|---|---|---|
| `state` | 状态原子（`Entity.attr=value`） | `Order.status=PAID`、`Session.authed_as=<authed>` |
| `action` | 端点动作 | `POST /orders/<id>/ship` |

### 5.3 边

| 边 | 含义 | 方向 |
|---|---|---|
| `requires` | 动作执行的前置条件（多条=AND） | action→state |
| `produces` | 动作产生的状态 | action→state |
| `invariant` | 同变量状态原子间的合法/非法迁移 | state→state（legal=true/false） |

### 5.4 与图①的关系

SSTG 是图①在状态空间的**投影**——把块级的锚点提升到端点粒度，形成可规划的状态图。规划器在 SSTG 上反向搜索链。

---

## 6. 语义判断链（零白名单）

```
语法形状挖候选（零框架知识，宁多勿漏）
  · 属性赋值: user.password = <expr>
  · 下标写:   session["k"] = v / data["key"] = v
  · 下标比较: if row["status"] != "PAID":
  · 下标真值: if row["used"]:
  · 构造调用: newBook = Book(title=..., secret=...)
  · 调用形状: token = make_token(...)
        ↓
LLM label（六类标签 + rationale）         ← 语义的唯一裁判
  authn / authz / ownership / money / workflow / single_use / irrelevant
        ↓
确定性选择器（固定类→策略，无阈值）
  authn/authz/ownership/money/single_use → 直入图
  workflow → 需值域 ≥ 2
  irrelevant → 出局
        ↓
LLM relate（auth_chains + 关系事实）
  · auth_chains: 哪个端点产/需/清认证状态
  · extra_transitions: 静态配对漏掉的迁移
  · role_hierarchy: 角色层级
  · cross_entity_invariants: 跨实体不变式
        ↓  apply → SSTG
WATCH 运行时校准（信道已通，回流待接）
```

**关键设计**：挖掘层和选择层**零安全知识**；安全判断只发生在 LLM 标注层，每条判断带 rationale（可审计）。

---

## 7. 三图的对应关系总结

```
┌─────────────────────────────────────────────────────────┐
│  图① 代码控制流图（G_xfg）                              │
│  ┌───────────────────────────────────────────────────┐  │
│  │ 节点: 基本块/函数/动作/库叶                        │  │
│  │ 边:   控制转移 + 调用 + 状态虚线                    │  │
│  │ 锚点: pre/eff/guard/sink 钉在块上                  │  │
│  └───────────────────────────────────────────────────┘  │
│                         ↓ ④门条件(路径分析,桥梁)         │
│                         ↓ ⑤数据流(def-use,值的传播)      │
│  图③ 状态图（SSTG）                                     │
│  ┌───────────────────────────────────────────────────┐  │
│  │ 节点: 状态原子(Entity.attr=value) + 动作           │  │
│  │ 边:   requires / produces / invariant              │  │
│  │ 语义: LLM 标签(六类) + auth 链                     │  │
│  └───────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

---

## 8. 代码位置对照

| 组件 | 文件 | 职责 |
|---|---|---|
| 语法挖掘 | `analyzer/patterns.py` | 纯语法候选（零名字规则） |
| 框架适配 | `analyzer/flask_adapter.py` / `openapi_adapter.py` / `django_adapter.py` | 路由→函数定位 |
| 调用图 | `analyzer/pycg_bridge.py` | 入口探测→隔离跑 PyCG→合并→缓存 |
| 过程内 CFG | `analyzer/cfg.py` `build_cfg()` | 基本块切分+边 |
| 锚点钉块 | `analyzer/cfg.py` `attach_anchors()` | SA-PR 事实按行范围钉块 |
| 门条件 | `analyzer/cfg.py` `block_gates()` | 路径可达性分析（may） |
| 数据流 | `analyzer/cfg.py` `reaching_definitions()` / `anchor_param_influence()` | 到达定值+参数传播 |
| G_xfg 装配 | `analyzer/cfg.py` `build_xfg()` | 调用图骨架+CFG 展开+锚点+状态虚线 |
| LLM 标注 | `llm/lifting.py` `label()` | 六类标签+rationale（缓存优先） |
| LLM relate | `llm/lifting.py` `relate()` | auth_chains+关系事实（缓存优先） |
| 选择器 | `analyzer/selector.py` | 固定类→策略（无阈值） |
| SSTG 构建 | `graph/builder.py` `build_sstg()` | 状态原子+动作+requires/produces/invariant |
| SVG 渲染 | `analyzer/svg.py` | 块级 CFG 图+函数级 XFG 图 |

---

## 9. 已验证的数字（2026-09-22 更新）

### 9.1 静态分析

| 靶标 | 框架 | 端点 | 调用图 | XFG | SEC |
|---|---|---|---|---|---|
| vulnshop | Flask | 11 | 49 节点 | 174 节点 | **6/6**（LLM 判） |
| VAmPI | connexion | 14 | 48 节点 | 5 节点 | 7 |
| FlaskBB 2.1 | Flask | 147 | 206 节点 | 175 节点 | 3 |
| PyGoat | Django | 229 | 338 节点 | 969 节点 | 3 |

### 9.2 代码覆盖率基线（vulnshop，110 个基本块）

| 配置 | 覆盖率 | 说明 | 论文对应 |
|---|---|---|---|
| **B0: 裸 Atheris / 盲打** | **28%** (31/110 块) | 每端点发一次请求，无 auth token。所有 authed 端点返回 401，业务逻辑代码未执行。**这是基线。** | B0 |
| B1: Atheris + auth 链 | 51% (56/110) | 自动发现需登录后构造 register→login 链，业务逻辑的门打开 | B1 |
| B2: StateForge 全家 | **70%** (77/110) | guided_explorer 定向探索：缺口分析→变异参数→自动调 ship/coupon 等端点 | B2 |
| 理论上限 | ~90% | 剩余 30% 为深层错误分支（空 items、负数 price）和 admin-only 路径 | — |

**核心主张的实验依据**：28% → 70% 的差距（42 个百分点）就是状态门的价值——这段差距正是代码距离定向的 fuzzer（Predator 系）够不到的，因为它们到不了门后面。

### 9.3 Atheris 覆盖引导 fuzzing（ANOTA 上修通）

```
cov: 11  corp: 5  NEW×5  exec/s: 3,933  121,925 runs/31s
```

修通方式：
- `instrument_func(视图函数)` 替代 `instrument_all()`（后者把 ANOTA 内部模块也插桩→冲突）
- `full_dispatch_request()` 替代 `CLIENT.open()`（后者经过未插桩的 Werkzeug 中间件→覆盖信号丢失）

### 9.4 覆盖率为什么停在 70%

剩余 30% 的块按阻挡原因分类：

| 原因 | 块数（约） | 到达条件 |
|---|---|---|
| 需 Order.status=PAID | ~8 | 先 create→pay，再 ship |
| 需 admin 角色 | ~5 | 提权或用 admin 账号 |
| 需特定非法输入 | ~10 | 空 items、负数 price、不存在的 ID |
| 需优惠券状态 | ~5 | 已用券（409）/ 不存在的券（404） |
| 防御性代码 | ~5 | GUARDS=1 分支（fixed 变体专属） |

52 测试全绿。
