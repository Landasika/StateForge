# Phase-1:结构执行图收尾(Structural Execution Graph — Close-out)

状态:已实施(2026-09-23,分支 phase1-structural);验收见 tests/static/test_buy_acceptance.py(3/3)+ 全量 97 passed。
上游:plans/design-v2-state-reachability.md(T+11/T+12 依赖此件收尾)。
来源:外部顾问版 Phase-1 规格(25 节)经代码对账后修正;对账表见 §2。

---

## 1. 目标

建立可信的 **Source → Function CFG → ICFG → Runtime Trace → BB/Edge 覆盖 → Frontier** 链路。
收尾后必须能回答(对任意 endpoint):

1. 调用了哪些函数?调用发生在哪个 BB?
2. callee 结束后返回到 caller 哪里?
3. 某次 HTTP 请求实际执行了哪些 BB、顺序如何?
4. 哪些边走过、各多少次、几个请求走过?
5. 哪些静态边从已覆盖区通向未覆盖区(frontier)?

不做:SSTG 改进、状态语义、LLM、智能参数生成、路径求解(见 §8 NON-GOALS)。

## 2. 对账:外部规格 vs 仓库事实

### 2.1 规格指控、代码证实(但大多已在 `static/` 新管线修复)

| 指控 | 位置 | 现状 |
|---|---|---|
| ast.walk 块归属 bug | `analyzer/cfg.py:177` | 属实,但仅影响旧 XFG 语义管线;`static/cfg.py` 已按 test-only 块修复 |
| CALL fallback 挂首块 | `analyzer/cfg.py:284` | 属实(旧管线);`static/icfg.py:68-73` 已改为 unresolved 记录,无假边 |
| break/continue 统一 terminal | `analyzer/cfg.py:99` | 属实(旧管线);`static/cfg.py:99-113` 已正确连 break→loop_exit、continue→header |
| 块 ID 不稳定 | `analyzer/cfg.py:60` | 属实(旧管线);`static/cfg.py:152-161` 已用 `file::func::lo-hi::kind::n` |
| CALL 无 RETURN 配对 | `analyzer/cfg.py:270-284` | 属实(旧管线);`static/icfg.py:87-91` 已加 return 边 |

### 2.2 规格要"新建"、实际已存在

| 规格要求 | 实际 |
|---|---|
| 新增 core/program_ir.py | `core/ir.py`(BasicBlock/FlowEdge/FunctionIR/ProgramIR,FlowEdge 含 kind=call/return、callsite_line、callee) |
| 新增 analyzer/icfg.py | `static/icfg.py`(build_icfg + unresolved_calls + callgraph 可选) |
| 新增 trace_mapping.py | `dynamic/mapper.py`(MappedTrace:block_hits/edge_hits/edge_kinds/unresolved) |
| 重做 tracer(只存 covered_lines) | 从未如此;`dynamic/tracer.py` 已是 call/line/return + request 隔离 + JSONL 增量落盘(T6-5/T6-6) |
| frontier/gap_analysis | `feedback/frontier.py`(Frontier:src/dst/kind/distance)+ `feedback/distance.py` + `feedback/engine.py`(FeedbackReport) |
| analyze_program 无 LLM 入口 | `cli.py analyze`(imports 无 llm),默认产出 `results/structural/program_ir.json` + `summary.json` |
| coverage_viz.py / guided_explorer.py / smart_params | 不存在(设计文档中的计划名,被外部规格误当现状) |

### 2.3 真实剩余缺口(本 spec 的工作项)

1. `static/icfg.py:62` `ast.walk(node)` 下钻嵌套 def/lambda → **嵌套函数体内的 call 误归属外层块**
2. 同一 BB 内多个 call:call 边全部从该块出发、return 边连该块全部 normal 后继 → **执行顺序不保真**;branch 块内 call 会产生双向假 return 边(过近似)
3. tracer 无 `exception` 事件
4. 覆盖无 per-request 维度(hit_count 有,request_count 无)
5. 无静态+动态叠图可视化
6. `analyzer/cfg.py` 旧管线四个 bug 仍在,污染 attach_anchors 的锚点归属(语义层最终挂接对象)
7. `static/` 管线测试不足(现有 tests/analyzer/ 测的是旧管线)
8. program_ir.json 无 source revision 标识(缓存失效判据)

## 3. 基线资产(不动)

```
core/ir.py            ProgramIR 契约(无 web/状态/安全概念)
static/cfg.py         单函数 CFG(ownership 已修,178 行)
static/icfg.py        build_icfg(call/return/unresolved,114 行)
dynamic/tracer.py     TraceSession(call/line/return,request 隔离)
dynamic/mapper.py     map_trace → MappedTrace
dynamic/coverage.py   block/edge/call ratio
feedback/             engine/frontier/distance/overlay
cli.py                analyze / feedback / experiment 三命令
analyzer/pycg_bridge.py  PyCG 桥(仅加接口,不重写)
```

## 4. 工作项

### P1-1 icfg 调用归属修复(缺口 1)
`static/icfg.py` 的 callsite 收集改为 ownership-aware 遍历:遍历函数体 AST 时
**不下钻** `FunctionDef/AsyncFunctionDef/Lambda/ClassDef` 的体(嵌套函数是独立
FunctionIR,由自身遍历覆盖)。实现为 `_iter_owned_calls(fn_node)` 生成器。

### P1-2 同块多 call 保序(缺口 2,方案 B:synthetic 节点)
在 `core/ir.py` 增加两类合成节点 kind:`callsite` / `returnsite`。
`build_icfg` 对块内 N 个内部 call 按语句顺序生成链:

```
B ─flow→ CS#1 ─call→ callee1.entry   callee1.exit ─return→ RS#1
RS#1 ─flow→ CS#2 ─call→ callee2.entry  callee2.exit ─return→ RS#2 ─flow→ succ(B)
```

- call 是块末语句(`return helper()`):RS#N ─flow→ caller.exit(沿用现有特判)
- external callee(无 CFG):保持现有 call-ext 叶子,不建合成节点
- CS/RS 节点作为 BasicBlock(kind=callsite/returnsite)写入 ir.blocks,
  复用既有的 covered_blocks/hit_count 逻辑,消费方无感
- block→succ 边保留原 CFG 边(true/false 分支语义不破坏,合成链挂在块与后继之间)
- mapper 对齐:动态 call/return 事件映射到 CS/RS 节点(line 匹配优先,合成节点
  携带 callsite_line)
- 兼容:消费方(feedback/engine)按 kind 过滤边,合成链各边 kind 仍属既有
  {flow, call, return},`find_frontiers` 的过滤集合无需改动

### P1-3 tracer 补 exception 事件(缺口 3)
`dynamic/tracer.py._trace` 的事件白名单 `{call, line, return}` 增补 `exception`;
TraceEvent.event_type 记 "exception"。mapper 对 exception 事件只计数不映射
(动态属性,不产生静态边)。

### P1-4 request 维度覆盖(缺口 4)
- `Overlay` 增 `block_request_count: dict[str, int]`、`edge_request_count`
- `build_overlay(ir, trace, request_ids)` 按 TraceEvent.request_id 分组统计
- `FeedbackReport` 增 `request_count`(总请求数);`cli.py feedback` payload 增字段
- Frontier 不改结构(Phase-2 再做 per-frontier 归因)

### P1-5 可视化(缺口 5)
新增 `stateforge/viz/coverage_dot.py`:输入 program_ir.json + feedback.json,
输出 dot/svg——覆盖块着色(hit_count 分档)、未覆盖块灰、frontier 边红色加粗、
CS/RS 节点小号渲染。cli 增 `viz` 子命令。沿用 `results/` 输出约定
(不引入 .stateforge/,对齐现有 cli 默认)。

### P1-6 旧管线修复:analyzer/cfg.py 委托 static/cfg.py(缺口 6)
`analyzer/cfg.py` 的 `build_cfg`/块构建改为内部调用 `static/cfg.py` 的
`build_function_cfg`,按旧调用方(attach_anchors/build_xfg/svg/test_cfg.py)
所需形状包装返回(networkx 图 + stmts 属性)。效果:锚点归属继承正确 ownership;
**不删除/不改任何语义数据结构**(StateCandidate/StateVar/Effect/…/state-link 原样)。
build_xfg 的 call 归属同步换用修好的块结构,消除 `sites or [首块]` fallback
(找不到就跳过并记 metadata,与 icfg 的 unresolved 口径一致)。

### P1-7 缓存标识(缺口 8)
`build_icfg` 写 `metadata["source_hash"]`(目标根下所有 .py 的
size+mtime 聚合哈希);cli `feedback` 加载 ir 时 hash 不符则警告。

### P1-8 测试与验收(缺口 7)
新增(放 `tests/static/`、`tests/dynamic/`、`tests/feedback/`):

```
test_cfg_if_else / loop / break_continue / return / try
test_icfg_simple_call / nested_call / multiple_calls_order / return_edge
test_icfg_nested_function_ownership      ← P1-1 回归
test_icfg_unresolved
test_trace_exception / request_isolation / map_callsite
test_overlay_request_count
test_frontier_kind_flow
test_viz_smoke
```

回归保护:`if admin: delete_user()` —— delete_user 的 callsite 块必须**不是**
condition 块(在 static 与 analyzer 两条管线各测一次)。

验收 fixture(`/buy` 最小 Flask app,tests/fixtures/buy_app.py):

```python
@app.post("/buy")
def buy():
    user = get_user()
    if not user:
        return "unauthorized", 401
    return checkout(user)

def checkout(user):
    if user.balance > 100:
        return charge(user)
    return "insufficient"
```

验收断言(静态 + 动态 balance=50 一条请求):
- ICFG 含 buy→get_user、buy→checkout、checkout→charge 的 call 边与配对 return 边(CS/RS 链)
- 动态:checkout 条件块的 false 边 hit≥1、true 边 hit=0
- frontier 恰含 `checkout条件块 --true--> (charge callsite 链)` 一条
- `cli.py experiment` 端到端跑通同一断言

## 5. 关键设计决策记录

| 决策 | 选择 | 理由 |
|---|---|---|
| 多 call 保序 | 合成 CS/RS 节点(规格方案 B) | 不破坏现有 CFG builder;静态路径搜索获得真实顺序 |
| return 边语义 | 只连 RS→(下一 CS 或 succ(B)),不再连"所有后继" | 消除 branch 块双向假边 |
| exception 静态边 | 不建(保留 Try 的 flow+condition 表示) | 避免图爆炸;动态 exception 仅计数 |
| 旧管线处置 | 委托而非重写 | 黑名单要求保留语义层;bug 修在结构源头一处 |
| 缓存目录 | 沿用 results/(cli 默认) | 已有约定,不引入第二套 |
| 块 ID | 不改(已是稳定形式) | file::func::lo-hi::kind::n 满足规格 §六 |

## 6. 修改文件清单(预期)

```
改  static/icfg.py          P1-1 P1-2 P1-7
改  core/ir.py              P1-2(kind 文档化 callsite/returnsite;无 schema 破坏)
改  dynamic/tracer.py       P1-3
改  dynamic/mapper.py       P1-2(CS/RS 映射)P1-3(exception 计数)
改  feedback/overlay.py     P1-4
改  feedback/engine.py      P1-4
改  feedback/frontier.py    无需改动(边 kind 集合已兼容;如实现中确需调整,在此登记)
改  cli.py                  P1-4 payload / P1-5 viz 子命令 / P1-7 警告
改  analyzer/cfg.py         P1-6(委托 static/cfg.py)
增  stateforge/viz/coverage_dot.py
增  tests/…(§4 P1-8 清单)、tests/fixtures/buy_app.py
```

删除文件:**0**。

## 7. 黑名单(PHASE-1 CHANGE BLACKLIST,照抄外部规格)

禁止删除/重构/重新设计:StateCandidate、StateVar、Effect、Precondition、
Guard、Sink、Invariant、SSTG/state graph、snapshot 核心、planner 核心、
executor 核心、LLM provider/lifting 核心、auth_baseline 业务逻辑、
Flask/Django/OpenAPI adapter 语义抽取(极小兼容改动除外)。
禁止删除:attach_anchors、state-link、anchor_param_influence、block_gates、
guided_explorer、smart_params/mutation(**注:后三者当前不存在,条款防御性保留**)。
禁止:全项目格式化;与 ICFG/runtime trace 无关的目录级重构;改 AppModel schema
塞 CFG/ICFG;ProgramIR 依赖 LLM;静态 ICFG 依赖状态语义;state-link 入基础
ICFG;DATA_DEP/STATE_DEP 作为路径搜索默认边;删未覆盖静态边;把"未动态观察"
当"静态分析错误";为过测试硬编码目标业务信息(route/username/coupon/amount/role 等)。

第 15-20 条语义:**保留而非删除,只是 Phase-1 不使用**。

## 8. NON-GOALS

SSTG 完成;StateVar 精确推理;Guard semantic lifting;LLM 解释路径;自动生成
HTTP 参数;SMT/符号执行;自动突破 frontier;认证推理;snapshot 状态搜索;
exploit 生成;planner 架构修改。

## 9. 执行顺序

```
P1-1 归属修复 → 测试
P1-2 CS/RS 保序 → 测试(mapper 对齐)
P1-3 exception 事件 → 测试
P1-4 request 维度 → 测试
P1-5 viz → smoke
P1-6 旧管线委托 → 旧测试(test_cfg.py 全量)回归
P1-7 source_hash
P1-8 验收 fixture 端到端 + 全量回归
```

每步完成即跑相关测试再进下一步;禁止一次性大改。

## 10. 风险与已知过近似(诚实条款)

- return 链对 `return f(g(x))` 这类嵌套调用:CS 顺序按语句序,嵌套表达式内的
  真实求值序(g 先于 f)在块级图上不区分——记为块粒度限制
- external callee(call-ext)无 return 边:库函数返回点不可知,Phase-1 接受
- exception 静态边缺失:Try 用 flow+condition 近似(现状保留)
- 动态 line→CS/RS 映射在优化(CPython 3.10 无字节码重排)下可靠;若未来换
  3.11+ 需重审 tracer

### 实施中新增的记录(2026-09-23)

- **优于 spec 原文**:RS→后继边保留原 true/false kind(替代 spec 写的统一
  flow),frontier 的分支语义因此完整
- mapper:同块多 call 第二次进入时,`RS1→host`、`host→CS2` 邻接对无静态边
  (block 级真实执行序),edge_kinds 不标注——非缺陷,块粒度表达
- **修复既有 bug**:TraceSession(root=None) 时 tracer 追踪自身帧导致自锁
  死锁(P1-3 测试暴露);现按 co_filename 自排除
- settrace 只对之后进入的函数帧生效:模块级帧无事件(harness 一律函数态)
- load_events 的 JSONL 格式 = "一行一输入记录"(request_id+events 嵌套,
  atheris_runner 产物);单事件一行的写法不支持
- 旧管线委托后 build_cfg 块 ID 变为稳定格式,XFG 节点名相应变化(svg 导出
  兼容,无消费方依赖旧 ID 格式)
