# StateForge 深度代码审计报告

审计日期：2026-11-09。基线提交：`2479351`。工作目录：`/home/landasika/icassp`。

## 结论与范围

框架具有明确的模块边界、生命周期契约和较完整的测试体系，但当前覆盖映射、HTTP 执行证据、快照分支和实验来源统计存在可复现错误。涉及这些路径的覆盖增长、目标验证和独立实验结果，需要在修复后重新验证。本报告不推断所有既有实验均错误，也不把研究原型的所有未支持语法都定义为安全漏洞。

本次重点审阅自有核心模块：静态分析、动态轨迹映射、状态目标求值、规划桥接、HTTP 执行和冷重放、快照恢复、LLM 边界与多轮实验。自有核心 Python 文件共 200 个、31,158 行；测试 Python 文件共 154 个。这是代码规模统计，不代表逐行穷举覆盖。external、ANOTA 安装目录与故意易受攻击靶标未作为框架自身漏洞清单。

未修改业务实现；仅增加报告与复现材料。所有新增反例使用临时目录、合成凭据、本地测试客户端或 Unix socket，不调用真实模型和外部业务服务。默认回归屏蔽模型凭据。

严重度按本项目影响定义：**高/P1** 为错误覆盖、错误验证、状态隔离破坏、实验结论明显失真或凭据暴露；**中/P2** 为条件性证据错误、错误规划或运行可靠性问题。不是 CVSS 远程可利用性评分。

## 结果索引

| ID | 严重度 | 已确认问题 |
|---|---|---|
| F01 | 高 | 同名方法映射到未执行方法的入口、出口 |
| F02 | 高 | 返回点跨调用方错配，同行及循环调用丢失调用证据 |
| F03 | 高 | with/async with 正文没有构建控制流 |
| F04 | 高 | 方法内部的可解析调用没有进入 ICFG 接线 |
| F05 | 高 | 短路调用被强制串联，真实跳过路径缺边 |
| F06 | 高 | 子快照冻结上一叶进程遗留的数据库状态 |
| F07 | 高 | HTTP 冷重放不核对源码指纹 |
| F08 | 高 | 非 POST 表单请求实际发送 POST，证据仍记录原动词 |
| F09 | 高 | NovelSeedGain 把 SF 注入种子视作 native 独立发现 |
| F10 | 高，条件触发 | 新实验复用输出目录时静默继承旧覆盖和语料 |
| F11 | 高，条件触发 | 登录凭据写入普通检查点及重放诊断命令 |
| F12 | 中 | SQLite 读事务可使恢复持续等待，未实现有界失败 |
| F13 | 中 | 控制协议把流式 socket 的一次读取当完整 JSON 消息 |
| F14 | 中 | 跨文件同名 view 匹配到错误端点与计划 |
| F15 | 中，条件触发 | 矛盾认证/实体观测仍可生成 VERIFIED_ACHIEVED |
| F16 | 中 | overlay 的 covered_edges 包含图外邻接对 |

共 **16 项：11 高、5 中**。相邻根因已合并，避免按每个现象重复计数。

## 逐项证据与修复建议

### F01：同名方法覆盖映射串线

位置：[mapper.py:36](../../../stateforge/runtime/mapper.py:36)、[tracer.py:78](../../../stateforge/runtime/tracer.py:78)。

`function_by_loc` 仅以文件和末级函数名为键，后写同名方法覆盖先前方法；轨迹仅记录 `co_name`。在同文件 A.run / B.run 的例子里，只执行 B.run，映射却包含 A.run 的 entry、exit，以及 B.run 正文。

实测：`METHOD_UNEXECUTED_A_ENTRY_COVERED True`。这同时制造未执行覆盖、遗漏正确覆盖，会传递到结构反馈和目标块判定。

建议贯穿静态/动态保存完整词法函数身份，增加 code firstlineno 或 qualified name；旧轨迹有歧义时报告 unresolved，不能默选。补同文件同名方法与嵌套同名函数反例。

### F02：调用与返回配对缺少完整调用点身份

位置：[mapper.py:55](../../../stateforge/runtime/mapper.py:55)、[mapper.py:109](../../../stateforge/runtime/mapper.py:109)、[mapper.py:119](../../../stateforge/runtime/mapper.py:119)。

RS 配对全局查询仅比较 callee 和 callsite_line，不比较调用方文件/函数及序号。a.py 和 b.py 在相同行调用 f，只运行 a，实际得到 a 的 CS、b 的 RS。该 callee.exit→b.RS 甚至是静态合法边，因此过滤图外边也不能消除此假覆盖。

实测：

```text
CROSSCALLER_ONLY_A_CALL_SITES ['a.py::a::2-2::callsite::1']
CROSSCALLER_ONLY_A_RETURN_SITES ['b.py::b::2-2::returnsite::1']
LOOP_REAL_F_CALLS 3
LOOP_CS_HITS 1
```

同一行 `f(); f()` 只留下 CS1、RS2；循环调用的 `chain_hits` 按整个 request 累加，超过静态链长后不再产生 CS。

建议 IR 明确保存 CS↔RS 配对；动态映射维护 caller frame 与每次语句求值的游标，callee 返回恢复 caller 上下文；同一静态调用点必须可重复命中。

### F03：with 正文控制流缺失

位置：[static/cfg.py:75](../../../stateforge/analysis/static/cfg.py:75)、[static/cfg.py:155](../../../stateforge/analysis/static/cfg.py:155)。

With/AsyncWith 没有分派处理，整个语句被当作单个普通语句，仅 with 起始行获得块。真实执行 `with cm: if flag: return 1` 时，分支和 return 行均 unresolved。

实测：`WITH_BRANCH_COUNT 0`、`WITH_UNRESOLVED_LINES [(3, 'line'), (4, 'line')]`。事务、锁、上下文管理是常见 Web 代码形态，遗漏影响覆盖分母、前沿和目标选择。

建议至少递归构建正常正文，显式保留分支和终止路径，再逐步建模 enter/exit 与异常抑制；不支持的形态应输出明确分析诊断。

### F04：方法体调用接线被跳过

位置：[static/icfg.py:72](../../../stateforge/analysis/static/icfg.py:72)、[static/icfg.py:91](../../../stateforge/analysis/static/icfg.py:91)。

收集 AST 时键为 `(app, A.run)`；第二遍对 `app.A.run` 做 rpartition 得 `(app.A, run)`，查询不到后直接 continue。因此方法内部调用模块级 `helper()`，即使可无歧义解析，也完全没有静态 call 边。

实测：`METHOD_HELPER_STATIC_CALLS []`、`METHOD_HELPER_REAL_CALLS 1`。

建议直接建立 qname→AST 映射并保留原始 module，不通过拆分类限定名反推模块。此处与“动态属性调用难解析”的已知限制不同。

### F05：短路求值路径从 ICFG 丢失

位置：[static/icfg.py:97](../../../stateforge/analysis/static/icfg.py:97)、[static/icfg.py:119](../../../stateforge/analysis/static/icfg.py:119)。

同块所有语法 call 被串成必经链，并移除 host 原后继。`if left() and right()` 中 left=False 时真实只调用 left，静态却强制经过 right，正确 false 路径变为 unmatched。

实测：`SHORT_CIRCUIT_REAL_CALLS ['h', 'left']`，`SHORT_CIRCUIT_UNMATCHED [('returnsite', 'terminal', 8)]`。

建议按 BoolOp/IfExp 的求值控制流建图，保留短路 skip 边；只有确定必执行的调用可直接串联。已有嵌套调用按列排序的文档限制并未解决该漏边。

### F06：子快照继承脏文件状态

位置：[snapshot/engine.py:97](../../../stateforge/runtime/snapshot/engine.py:97)，对照 [snapshot/engine.py:51](../../../stateforge/runtime/snapshot/engine.py:51)。

serve 在 fork 前恢复 L2；freeze_child 没有恢复父检查点模板，直接 fork 并在共享 live 文件上构建、捕获。前一叶退出时文件修改仍存在，下一次 freeze_child 因此把这些遗留数据带进新模板，而内存仍来自冻结父进程。

复现先建立空 root 模板，再模拟已完成叶写入 99，调用真实 `_handle_freeze`，build=0：

```text
ROOT_BASELINE_ROWS []
CHILD_BUILD_ZERO_ROWS [(99,)]
```

建议 freeze 和 serve 一样在 fork 前恢复父 L2，并协调同一 live DB 的操作互斥；补“serve 写入→freeze_child→child serve”及 sibling 切换测试。复现直接调用引擎，不依赖 ANOTA。

### F07：HTTP 冷重放跳过版本校验

位置：[http_checkpoint_replay.py:72](../../../stateforge/solving/http_checkpoint_replay.py:72)。微靶路径已有正确对照：[goal_checkpoint.py:172](../../../stateforge/solving/goal_checkpoint.py:172)。

HTTP 路径加载 checkpoint/IR 后直接执行，未比较 checkpoint.source_hash、IR 指纹和当前源码，也不核对 source_file 与目标块。修改函数体但保留行号，用旧 IR/checkpoint 在真实独立子进程重放，仍 `verified=True`。

实测：`HTTP_STALE_HASH changed=True verified=True returncode=0 error=None`。

建议提取共享检查器，执行前同时验证目标块、源文件和源码指纹；增加 HTTP 源码修改/错源文件反例。不要仅回显旧 hash 就把它当成已验证版本。

### F08：表单请求动词与真实执行不符

位置：[http_plan_executor.py:93](../../../stateforge/runtime/http_plan_executor.py:93)、[http_plan_executor.py:104](../../../stateforge/runtime/http_plan_executor.py:104)。

任何非 GET 且带 form 的请求都执行 `client.post`，但 request 记录仍写原始 method。DELETE/PUT/PATCH 表单会实际触发 POST 处理，并可以根据 POST 覆盖判为 verified。

实测：`actual=('POST', '/item', {'confirm': 'yes'})`，`recorded.method='DELETE'`，`verified=True`。

建议仅 POST 使用 post 快捷方法，其余动词正确编码 form 后通过 generic 发送；请求证据必须反映实际分派。现有测试仅覆盖 POST form、PATCH JSON。

### F09：SF 种子来源污染 native 统计

位置：[epoch.py:59](../../../stateforge/evaluation/experiment/epoch.py:59)、[epoch.py:363](../../../stateforge/evaluation/experiment/epoch.py:363)、[epoch.py:377](../../../stateforge/evaluation/experiment/epoch.py:377)。

SF spool 导入下一轮输入目录，再全量复制回 lf_corpus；末轮 `_novel_seed_gain` 却将 lf_corpus 全部内容当 native。仅生成一条 SF 候选，native 从未执行，经过真实 export/copy 结转后增益已归零。

```text
NSG_IMPORTED_ONLY {'sf_candidates_total': 1, 'sf_already_in_native': 1,
'sf_unique_vs_native': 0, 'novel_seed_gain': 0.0}
```

建议记录每轮初始输入 hash 和 native 新发现 lineage，排除注入来源，或使用独立配对 native 对照集合。该问题影响增益解释，不说明所有覆盖计数均错误。

### F10：重新运行混入旧实验初态

位置：[epoch.py:316](../../../stateforge/evaluation/experiment/epoch.py:316)、[entry_driver.py:305](../../../stateforge/fuzzing/entry_driver.py:305)。

工作目录 exist_ok，语料不清空；driver 见 global_state 就恢复，无 run/IR/seed 身份检查。改变 seed、配置或源码后重复使用同 out，旧覆盖和候选仍可能进入新结果。

复现保留旧语料及 prior-only-block，再调用 run_epochs，仅替换实际 fuzz 子进程边界：`NEW_RUN_REUSED {'old_corpus': True, 'old_state': {'blocks': ['prior-only-block'], 'edges': []}}`。真实 driver 的无条件加载已代码复核。

建议默认拒绝非空新 run 目录；显式 resume 校验 target、IR、seed、arm 和 schema，使用 run UUID。跨 epoch 的合法恢复应保留，跨 run 不应隐式恢复。

### F11：凭据明文进入可分享产物

位置：[http_plan_executor.py:81](../../../stateforge/runtime/http_plan_executor.py:81)、[goal_checkpoint.py:126](../../../stateforge/solving/goal_checkpoint.py:126)、[http_checkpoint_replay.py:110](../../../stateforge/solving/http_checkpoint_replay.py:110)。

环境凭据合入请求后，execution.request 和 checkpoint.steps 原样保存密码。冷重放又把 password 放入进程 argv，并将完整 command 写回 evidence JSON。复现仅用合成密码，checkpoint 中可找到原值，当前 umask 下权限为 0664；具体暴露对象取决于目录权限和报告分享范围。

建议 checkpoint 保存 secret 引用及运行时绑定位置；可读报告统一脱敏，原始必要私密材料以 0600 隔离保存；子进程经受控 stdin/专用 fd 接收凭据，不将其写进 argv 或回显 command。不要直接删值导致不可重放，应建立明确的 secret 重绑定协议。

### F12：读锁使恢复无限重试

位置：[l2_sqlite.py:175](../../../stateforge/runtime/snapshot/l2_sqlite.py:175)、[l2_sqlite.py:191](../../../stateforge/runtime/snapshot/l2_sqlite.py:191)。

`BEGIN IMMEDIATE` 可以在其他连接持有 SHARED 读锁时成功，但 backup 提交需要更强锁；sqlite backup 会继续重试，timeout=0 不会限制 backup 的循环。独立子进程在真实读事务存在时超过 2 秒仍未返回，被复现驱动超时终止。

实测：`READ_LOCK_RESTORE: HANG beyond 2s despite timeout=0`。这是恢复无界等待，不是已证明数据损坏；主要在靶侧连接未彻底关闭时触发，恰是 fail-closed 要兜底的边界。

建议 backup progress 回调设单调时钟 deadline，或在隔离子进程中执行可终止恢复；锁失败统一转 SnapshotConsistencyError。单独探测写锁不能保证后续 backup 不被读锁阻塞。

### F13：Unix STREAM 消息缺少分帧

位置：[protocol.py:18](../../../stateforge/runtime/snapshot/protocol.py:18)、[protocol.py:26](../../../stateforge/runtime/snapshot/protocol.py:26)。

_send 添加换行，但 _recv 对一次 recv_fds 的全部字节直接 json.loads。STREAM 不保留发送边界，半包和多个包粘连都会 JSONDecodeError；65536 字节以上消息也会被截断。

实测分别发送 `{"cmd":` 和两个完整换行 JSON：`FRAGMENTED_CONTROL JSONDecodeError`、`COALESCED_CONTROL JSONDecodeError`。

建议采用长度前缀或缓冲逐行解码，保留剩余字节并正确关联 SCM_RIGHTS；设置大小上限并关闭未消费 fd。当前请求响应节奏降低粘包概率，但不能替代分帧。

### F14：同名 view 生成错误计划

位置：[goal_app_bridge.py:110](../../../stateforge/state/goal_app_bridge.py:110)。

只比较末级函数名，忽略文件、模块。a.edit 在模型中先于 b.edit，对 b 的目标生成 a 的前置条件和最终路由。

实测目标 `b.py::edit::2-2::terminal::1`，结果 `constraints=['Session.authed_as']`，`steps=['POST /login', 'GET /a/edit']`，而 b 的条件是 db.b.status=READY。

建议按 resolved 文件加完整词法身份匹配，歧义拒绝。执行模式通常会因目标未覆盖而失败，因此本项按错误计划评为中，不声称默认出现验证假阳性。

### F15：矛盾证据仍生成完整 witness

位置：[goal_evaluation.py:129](../../../stateforge/state/goal_evaluation.py:129)、[goal_evaluation.py:141](../../../stateforge/state/goal_evaluation.py:141)。

认证特殊分支绕过字段冲突检查，RequestAuth.subject 同点 alice、bob 采用最后一条；相同事实排列顺序不同，结果分别为 VERIFIED_ACHIEVED 和 VERIFIED_NOT_ACHIEVED。另一分支允许 exists=False/MISSING 与同实体字段同时进入 absent 和实体快照，纯字段目标仍产生成功 witness。

```text
AUTH_CONFLICT alice,bob ... status=VERIFIED_ACHIEVED witness=True
AUTH_CONFLICT bob,alice ... status=VERIFIED_NOT_ACHIEVED witness=False
EXISTENCE_CONFLICT absent=['users:bob'] ... status=VERIFIED_ACHIEVED
```

触发条件是插件或组合观测器提供矛盾批次，当前内置单探测通常不会产生它。建议统一 presence、auth 与字段的冲突检查，遇矛盾拒绝整个视图，补排列不变性测试。

### F16：公开反馈覆盖边口径不一致

位置：[overlay.py:22](../../../stateforge/fuzzing/feedback/overlay.py:22)。

overlay.covered_edges 直接用全部 trace.edge_hits，而合法覆盖计数使用静态边校验。短路反例中 `OVERLAY_NONSTATIC_EDGE_COUNT 1`，同产物不同字段出现不同“覆盖”定义。

建议 covered_edges 使用 observed_valid_edges / edge_kinds，原始执行邻接另存 observed_adjacency；若冻结协议需兼容，新增字段并迁移消费者。原始邻接对用于诊断是合理的，问题在于将其无区别暴露为覆盖边。

## 已披露限制及较小问题

- `try: return ... finally: cleanup` 的 finally 块从静态入口不可达。实测 return→finally 是 unmatched。旧 CFG 文档已披露 finally 顺序近似，本报告把它作为已有局限的实际影响，不计入 16 项新发现；生产型控制流支持应修复 pending return/raise/break/continue 经 finally 的建模。
- [runner.py:69](../../../stateforge/evaluation/experiment/runner.py:69) 读取 wall_total，而 run_epochs 返回 wall_span，末尾打印存在 KeyError 风险；代码键名不一致已确认，未跑长实验验证该 CLI 全路径。
- [four_arm.py:126](../../../stateforge/evaluation/experiment/four_arm.py:126) 将 float seconds 转 int，记录预算与实际传参可能不同，0.5 会成为 0；建议保留 float。
- LLM proposal parser 对 `[]`/`null` 抛 AttributeError，对错误字段类型不做完整校验。外层注入器捕获 solve 异常后记 PROVIDER_ERROR，故不升级为已证实主流程崩溃。建议统一结构校验并区分模型输出错误和网络失败。
- 语义缓存写入安装包目录、缓存键不含模型身份，属于部署可写性及实验溯源待增强项；本次未做只读 wheel 部署和多模型隔离复现，不列已确认高风险。

## 验证方法与复现

复现材料：[静态与实验](repro_static.py)、[目标与 HTTP](repro_goals.py)、[快照与协议](repro_runtime.py)、[合成凭据](repro_credentials.py)。在仓库根目录运行：

```sh
PYTHONPATH="$PWD" /tmp/ci-venv/bin/python docs/reports/audit-2026-11-09/repro_static.py
PYTHONPATH="$PWD" /tmp/ci-venv/bin/python docs/reports/audit-2026-11-09/repro_goals.py
PYTHONPATH="$PWD" /tmp/ci-venv/bin/python docs/reports/audit-2026-11-09/repro_runtime.py
PYTHONPATH="$PWD" /tmp/ci-venv/bin/python docs/reports/audit-2026-11-09/repro_credentials.py
```

其中 /tmp/ci-venv 是本次发现的既有环境，可换成安装项目测试依赖的 Python。运行时反例会创建并回收一个 fork 子进程；读锁例子使用 subprocess 超时自动回收。临时样本目录保留以便检查。

四份归档脚本已实际运行，均退出 0；三组主要输出已保存：[静态日志](repro_static.py.log)、[目标日志](repro_goals.py.log)、[运行时日志](repro_runtime.py.log)。报告中的文件引用与行号已检查有效。

既有 micro_benchmark：21/21 静态、5/5 动态均为 1.00。它的动态测试未覆盖本次新增的同名方法、同行重复调用、短路跳过及 with 正文组合，所以全绿与这些反例同时成立。

默认 pytest 已完成：**987 passed, 1 skipped, 26 deselected, 1 warning，687.69 秒，退出 0**。唯一 warning 为 PlanStep.json 遮蔽父类属性。26 个 ANOTA 标记用例按默认配置未执行。回归启动时屏蔽 ANTHROPIC、OPENAI、SF_LLM、STATEFORGE_LLM 凭据和配置，使用 /tmp/ci-venv/bin/python。没有执行 fresh-clone 安装 CI、全部 ANOTA 标记用例、20 个外部真实应用长时战役或付费 LLM 调用，因此不宣称这些路径已全部验证。

## 建议修复顺序

1. 先修 F01/F02/F06/F07/F08：保证“实际执行什么”和“验证了什么”一致，同时处理 F11 凭据产物。
2. 修 F03/F04/F05 并补反例：将 Python 常见作用域、上下文和短路语义纳入基准。
3. 修 F09/F10：明确语料来源与 run 身份，对受影响实验重新跑独立对照。
4. 修 F12/F13/F14/F15/F16：有界恢复、消息分帧、歧义拒绝、证据一致性和公开字段口径。

验收应同时包含有缺陷/修复后对照，明确检查未执行方法和未执行调用方不获得覆盖；错误动词、旧源码、矛盾观测不获 verified；子快照状态仅来自父模板及本次 build；native 增益不包含输入导入来源。
