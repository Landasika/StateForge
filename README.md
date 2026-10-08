# StateForge

**结构引导的 Web 应用模糊测试：从"看见哪里没覆盖"到"主动生成能过 if 的输入"。**

StateForge 在 Atheris（Python fuzzing 引擎）之上构建了多层反馈体系：静态执行图（ICFG）→ 运行时轨迹映射 → 覆盖前沿检测 → 分支约束求解 → LLM 语义推理生成种子。当传统 fuzzing 在某道分支门前停滞时，StateForge 分析门上的条件、追溯输入来源、调用 LLM 推导目标值、生成候选种子写回 Atheris 语料库，由 Atheris 继续变异探索。

```
Atheris（变异引擎，唯一执行者）
    │ bytes
    ▼
GenericWebAdapter（通用适配：bytes → JSON → HTTP 请求）
    │
    ▼
目标应用（Flask / Django test_client 进程内执行）
    │ sys.settrace
    ▼
TraceSession（逐请求隔离采集执行轨迹）
    │
    ▼
Mapper（轨迹 → 静态 ICFG 块/边映射，Phase-1 冻结）
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│ 反馈层                                                    │
│                                                           │
│  P2 结构新颖度：新块/新边 → 保留种子（零增益实证）           │
│  P3 前沿定向：距离+组合冠军+预算分配（零增益实证）           │
│  P4.1 确定性求解：Guard恢复→操作数→距离→溯源→约束→变异      │
│  P4.2 LLM 求解：停滞检测→切片→LLM→校验→种子注入            │
└───────────────────────┬─────────────────────────────────┘
                        │ 种子写入 corpus 目录
                        ▼
              Atheris 下一轮加载并变异
```

---

## 目录

- [快速开始](#快速开始)
- [项目阶段](#项目阶段)
- [模块详解](#模块详解)
- [实验结果](#实验结果)
- [冻结版本](#冻结版本)

---

## 快速开始

```bash
# 安装（唯一依赖源：pyproject.toml；atheris 编译需 clang）
pip install -e ".[dev,test,fuzz]"

# 跑全量测试（255 tests，26 个 anota 标记用例默认 deselect、在 ANOTA 解释器内另跑）
pytest

# 静态分析：构建 ICFG
python -m stateforge.cli analyze <目标目录> --out results/structural

# 运行时反馈：执行轨迹 → 覆盖/前沿
python -m stateforge.cli feedback results/structural/program_ir.json trace.jsonl --out feedback.json

# 可视化：静态+动态叠图
python -m stateforge.cli viz program_ir.json feedback.json --out coverage.dot
dot -Tsvg coverage.dot -o coverage.svg

# 微基准验证（21 用例，全部 1.00）
python -m stateforge.eval.micro_benchmark

# 四臂实验（A/B/C/D 对照）
python -m stateforge.experiment.four_arm --out results/experiment --runs 3 --epochs 3 --seconds 8
```

---

## 项目阶段

| 阶段 | 内容 | 状态 | 标签 |
|------|------|------|------|
| **Phase 1** | 静态/动态底座（ICFG + Trace + Mapper） | ✅ 冻结 | `phase1-freeze` |
| **Phase 2** | 结构新颖度反馈闭环 | ✅ 完成（零增益实证） | |
| **Phase 3** | 前沿定向反馈（距离+组合+预算） | ✅ 冻结（停止规则成立） | `phase3-structural-freeze` |
| **Phase 4.1** | 确定性分支求解 | ✅ 冻结（22/22 用例 100%） | |
| **Phase 4.2** | LLM 语义求解器 + 种子注入 | ✅ 完成 | `phase4.2-llm-loop-complete` |
| **Phase 4.3** | 状态可行性（多步序列） | ⬜ 未开始 | |

---

## 模块详解

### `stateforge/core/` — 数据契约

| 文件 | 职责 |
|------|------|
| `ir.py` | **ProgramIR**：结构 IR（BasicBlock、FlowEdge、FunctionIR），与语义层 AppModel 解耦。块 ID 稳定格式 `relpath::func::lo-hi::kind::n`；FlowEdge 携带 kind（flow/true/false/call/return）、callsite_line、callee |
| `model.py` | **AppModel**：语义 IR（StateCandidate、StateVar、Effect、Precondition、Guard、Sink），Phase-4.3 状态求解的数据基础 |
| `serialization.py` | ProgramIR 与 AppModel 的 JSON 序列化/反序列化 |

### `stateforge/static/` — 静态分析（Phase-1 冻结）

| 文件 | 职责 |
|------|------|
| `cfg.py` | **单函数 CFG 构建**：AST → 基本块（顺序/分支/循环/终止），break→loop_exit、continue→header 正确连边；稳定块 ID；Try/except 保守近似 |
| `icfg.py` | **过程间 CFG（ICFG）**：PyCG 调用图 + 函数 CFG → 全程序 ICFG；CS/RS 合成节点保证同块多调用保序；unresolved_calls 记录不造假边；`_iter_owned_calls` 确保 AST ownership（嵌套函数/lambda 不误归属）；`_source_hash` 缓存失效判据 |

### `stateforge/dynamic/` — 运行时采集与映射（Phase-1 冻结）

| 文件 | 职责 |
|------|------|
| `tracer.py` | **执行轨迹采集**：`sys.settrace` 的 call/line/return/exception 事件逐请求隔离；JSONL 增量落盘（抗 libFuzzer `_Exit`）；文件前缀缓存优化热路径；`_SELF_FILE` 自排除防死锁 |
| `mapper.py` | **轨迹 → ICFG 映射**：精确 `line_numbers` 优先、合成节点降权、range 兜底；调用栈逐请求清空；CS/RS 链对齐 |
| `coverage.py` | **覆盖度量**：block/edge/call ratio（分母 = ProgramIR 执行边总数） |
| `io.py` | **轨迹 I/O**：load_events 兼容 array/harness-object/JSONL 三格式；merge_events 跨文件去重并重编号 |

### `stateforge/feedback/` — 反馈引擎（Phase-2/3 冻结）

| 文件 | 职责 |
|------|------|
| `structural.py` | **P2 结构新颖度**：`S(x) = N_B(x) + N_E(x)`，先评分（对执行前快照）后更新全局；只计有静态支撑的边对 |
| `frontier_tracker.py` | **P3 前沿追踪**：Edge Frontier（源块已覆盖、边未走过）+ Node Frontier；版本化；生命周期事件（created→crossed→crossing_seconds） |
| `distance_oracle.py` | **P3 距离预言**：反向多源 BFS；按前沿版本缓存；UNREACHABLE 单列零分 |
| `frontier_feedback.py` | **P3.1 前沿定向反馈**：`S = NB + NE + 1/(1+D)`，λ=1 冻结；先评分后 commit |
| `frontier_portfolio.py` | **P3.2 前沿组合**：per-frontier 冠军（≤1/前沿）；Progress 事件驱动换冠军 |
| `frontier.py` | **前沿检测**：Node Frontier + Edge Frontier 定义 |
| `distance.py` | **最短路径距离**：反向 BFS（distance_to_targets） |
| `overlay.py` | **覆盖叠加层**：covered_blocks/edges/hits/request_count |
| `engine.py` | **反馈引擎**：compute_feedback(ir, events) → FeedbackReport（overlay + frontiers + distances + request_count） |
| `corpus.py` | **语料排序**：种子评分与排序 |

### `stateforge/feasibility/` — 分支可行性分析（Phase-4 核心）

| 文件 | 职责 |
|------|------|
| `guard_ir.py` | **BranchGuard IR**：guard_id/source_block/target_block/polarity/expression/op/lineno/col_offset/status |
| `guard_recovery.py` | **前沿 → Guard 恢复**：branch 块反解原文件 AST 的 test 表达式；支持 ==/!=/</<=/>/>=/truthy/not/In/NotIn；chained/BoolOp → UNSUPPORTED 不猜 |
| `branch_observer.py` | **运行时操作数观测**：AST 重写 if/while test → `_sf_cmp(gid, op, lhs, rhs)` 调用；操作数按原始求值序各求值一次；不支持表达式原样保留；gid 与静态恢复同键 |
| `branch_distance.py` | **门内梯度距离**：公式实验前冻结（Eq=|x−y|，Lt=max(0,x−y+1) 等）；desired-false = 相反谓词；d_norm = d/(d+1) 唯一口径；字符串 Eq 0/1 |
| `input_provenance.py` | **输入溯源（静态）**：QUERY/FORM/JSON/HEADER/COOKIE 五源 + int/float/str/strip/lower/upper 变换链；guard 前直线赋值；分支重定义→SOURCE_AMBIGUOUS；`.get(key, default)` 双参支持 |
| `enhanced_provenance.py` | **增强溯源**：②跨函数静态追踪（ICFG call 边→caller→实参→变量赋值→模式匹配）+ ③动态值关联（守卫操作数值 vs 种子字段值）；优先级：函数内 > 跨函数 > 值关联 > UNKNOWN |
| `branch_solver.py` | **约束求解**：冻结规则表（Eq→C、Lt→C−1、Gt→C+1、desired-false=补谓词）；RHS 输入先 canonicalize（_SWAP）再翻极性；字符串仅 Eq/Ne |
| `mutation_hint.py` | **MutationHint IR**：frontier_id/guard_id/source_kind/source_key/current/target/operator/polarity/reason/confidence/transform_chain |
| `semantic_slice.py` | **前沿语义切片**：七项 JSON（目标前沿/Guard AST/局部 CFG/后向数据流/输入源/运行时值+距离历史 M=10/失败突变史） |
| `plateau.py` | **覆盖平台检测**：W=2000 冻结；ΔBB=ΔEdge=ΔCrossing=0 → STAGNATED；滞回：仅 frontier crossing 解锁 |
| `stalled.py` | **停滞前沿发现**：N=100 冻结；源块 hit≥N ∧ 边未观测 ∧ guard 可恢复 |
| `stagnation.py` | **触发组合器**：Plateau ∧ Stalled ∧ DeterministicFailure → 触发并携带切片 |
| `proposal.py` | **MutationProposal IR**：五状态（PROPOSED/INSUFFICIENT_EVIDENCE/STATE_REQUIRED/UNSUPPORTED/INVALID_OUTPUT） |
| `llm_solver.py` | **LLM 调用**：prompt 冻结；JSON 解析（markdown 围栏剥离）；LLM 只产 proposal 不做 oracle |
| `proposal_validator.py` | **信任边界**：provenance 成功→严格对齐 source kind/key；失败→引号包裹 key 匹配 backward_dataflow 文本；operation 仅 REPLACE；不重复失败突变 |
| `proposal_trace.py` | **Proposal Trace**：JSONL 逐条 flush（trigger/distance before/after/latency/tokens） |
| `solve_loop.py` | **确定性求解循环**：frontier→guard→operand→provenance→solve→mutate→execute→mapper 裁决 FRONTIER_CROSSED；嵌套前沿迭代（F1→solve→cross→F2→solve→cross→target） |
| `llm_seed_injector.py` | **LLM 种子注入器**：停滞 frontier→切片→LLM→校验→种子写入 Atheris corpus（不执行目标）；UNSUPPORTED guard 不跳过；LLM 种子叠加（渐进开门） |

### `stateforge/fuzz/` — Fuzzing 集成

| 文件 | 职责 |
|------|------|
| `entry_driver.py` | **Atheris 子进程入口**：off/measure/feedback/frontier 四模式；选择性插桩（只打目标模块）；全局覆盖跨 epoch 持久化 |
| `instrumentation.py` | **选择性字节码插桩**：Instrumenter（code-key 去重防二次改写） |
| `corpus_manager.py` | **语料管理**：内容哈希去重；原始字节持久化；origin=NATIVE/LLM lineage 追踪 |
| `candidates.py` | **候选采集器**：逐输入捕获+评分+保留（P2 模式） |
| `frontier_collector.py` | **前沿采集器**：逐输入捕获+组合挑战+保留（P3.1 模式） |
| `constraint_mutator.py` | **约束变异器**：MutationHint → 新种子（深拷贝只改目标字段；QUERY/FORM/HEADER/COOKIE 字符串域、JSON 原生类型；LLM 可创建新字段） |

### `stateforge/fuzz/adapters/` — 目标适配层

| 文件 | 职责 |
|------|------|
| `base.py` | **TargetAdapter Protocol**：`prepare(atheris)` + `execute(data: bytes)` |
| `generic_web.py` | **通用 Web 适配器**（40 行）：bytes → JSON → HTTP 请求；自动检测 Flask(.open) vs Django(HTTP_前缀)；适配任意 WSGI 应用 |
| `callable.py` | **可调用适配器**：bytes → 函数参数 |
| `flask.py` | **Flask 适配器**：路由枚举+路径参数预填+字节驱动 body |
| `vulnshop.py` | **vulnshop 靶场适配**：预注册用户+Bearer 认证+路由 body 映射 |

### `stateforge/experiment/` — 实验基础设施

| 文件 | 职责 |
|------|------|
| `epoch.py` | **Epoch 控制**：固定时间预算起 Atheris 子进程；语料交接（B=libFuzzer 结转、C=SF∪结转、D=+LLM 轮间注入）；墙钟记录；跨 epoch 累计；NovelSeedGain |
| `runner.py` | **多臂实验**：A_native/A_measure/B/C/D1/D2R/D2P 七组同种子配对 |
| `campaign.py` | **正式战役**：N runs 配对种子 + AUC/分位曲线 + paired deltas + manifest |
| `recorder.py` | **实验记录器**：覆盖-时间采样 + 候选事件 + exec/feedback 分项计时 |
| `four_arm.py` | **四臂简化实验**：A/B/C/D 对照 |
| `plot_campaign.py` | **曲线图**：Coverage-vs-Time（dataviz 调色板过检） |

### `stateforge/eval/` — 评估基准

| 文件 | 职责 |
|------|------|
| `micro_benchmark.py` | **SF-Micro 真值基准**：21 用例静态 P&R 全 1.00 + 5 用例动态 trace-accuracy 全 1.00 |
| `branchsolve_report.py` | **BranchSolve 报告**：22 用例七指标（GuardRecovery/Operand/Provenance/Hint/Candidate/BranchSatisfaction/FrontierCrossing accuracy） |
| `benchmark_baseline.py` | **OWASP Benchmark 基线**：全仓 ICFG + flask test_client 采样 + 覆盖/前沿 |

### `stateforge/analyzer/` — 语义分析（Phase-4.3 素材）

| 文件 | 职责 |
|------|------|
| `analyzer.py` | **语义分析入口**：analyze(target) → AppModel（LLM lifting + state selection + invariant） |
| `patterns.py` | **候选模式匹配**：8 种数据流模式（直读/链式 JSON get 等）→ StateCandidate |
| `selector.py` | **确定性选择器**：固定类别→策略映射，无阈值 |
| `cfg.py` | **XFG 执行流图**（委托 static/cfg.py）：过程内 CFG + 锚点钉块 + 跨端点状态虚边 |
| `pycg_bridge.py` | **PyCG 桥接**：预生成调用图 JSON → ICFG callee 消歧 |

### `stateforge/llm/` — LLM 基础设施

| 文件 | 职责 |
|------|------|
| `provider.py` | **双协议 Provider**：OpenAI 兼容 + Anthropic；缓存+降级 |
| `lifting.py` | **语义提升**：label(candidates) 六类标签 + relate() 有界关系 |
| `real_provider.py` | **Real/Mock Provider**：env-var 配置（STATEFORGE_LLM_API_KEY 等）；MockLLMProvider 预设响应序列 |

### `stateforge/targets/` — 测试靶场

| 目录 | 应用 | 说明 |
|------|------|------|
| `vulnshop/` | 自建 Flask 靶场 | 11 路由，5 类 PoC（workflow_bypass/payment_bypass/privilege 等） |
| `vampi/` | OWASP VAmPI | 14 端点 Flask API |
| `dsvw/` | DSVW | ~20 端点 |
| `pygoat/` | PyGoat | Django 4.2 OWASP Top-10 |
| `realworld/` | RealWorld Conduit | generic_target.py（71 行 Atheris 靶函数） |
| `microplus/` | 微靶集合 | hash_gates/misleading_subtree/semantic_concat/role/url/s1/s2 |

### `stateforge/tests/` — 测试套件（255 tests）

| 目录 | 覆盖 |
|------|------|
| `tests/static/` | CFG/ICFG/ownership/callsite_chain/relative_ids/source_hash/buy_acceptance |
| `tests/dynamic/` | trace_exception/map_callsite/map_exact_ownership |
| `tests/feedback/` | structural/frontier/edge_frontier/request_counts |
| `tests/feasibility/` | guard_recovery/observer/distance/branch_solve/enhanced_provenance/stagnation |
| `tests/fuzz/` | corpus_manager |
| `tests/integration/` | llm_seed_injector/real_llm_smoke(gated) |
| `tests/structural/` | adapters/instrumentation/pipeline/trace_io/tracer_perf |
| `tests/viz/` | coverage_dot 格式校验 |

### `stateforge/fixtures/` — 真值基准数据

| 目录 | 内容 |
|------|------|
| `micro/` | SF-Micro 21 用例（if/loop/break/call/return/try/recursion 等）+ expected_icfg.json + expected_trace.json |
| `bs_micro/` | BS01-BS22 BranchSolve 真值（query/form/json/header/cookie/int/float/strip/嵌套/rhs/unsupported） |
| `ls_micro/` | LS01-LS04 LLM 三类（deterministic/transform/state_guard） |
| `llm_loop/` | T1-T6 + S1/S2（LLM 覆盖循环微靶） |
| `sf_branch_micro/` | Guard 恢复 10 用例 |
| `buy_app/` | /buy 验收最小 Flask 应用 |

### `stateforge/viz/` — 可视化

| 文件 | 职责 |
|------|------|
| `coverage_dot.py` | 静态+动态叠图：覆盖块着色/hit 分档、未覆盖灰、frontier 红粗、call/return 紫 |

### `stateforge/cli.py` — 命令行入口

| 命令 | 功能 |
|------|------|
| `analyze` | 构建 ICFG（无 LLM 依赖） |
| `feedback` | 轨迹 → 覆盖/前沿/距离 |
| `experiment` | 静态+动态一键 |
| `viz` | 叠图 dot 导出 |

---

## 实验结果

### SF-Micro 真值基准（Phase-1 冻结）

| 指标 | 结果 |
|------|------|
| 静态 CFG/CALL/RETURN Precision & Recall | **全部 1.00**（21 用例） |
| 动态 trace-accuracy | **全部 1.00**（5 用例，手写期望序列逐位一致） |

### 四臂消融战役（Phase-3 冻结，10 paired runs）

| 臂 | AUC median |
|----|-----------|
| C（结构新颖度） | 0.3251 |
| D1（+前沿距离） | 0.3329 |
| D2R（+组合重注入） | 0.3252 |
| D2P（+focused 预算） | 0.3272 |

**结论：纯结构路线零增益**——SF 新颖度与 native coverage 信号完全重叠（80/80 候选已在 native 语料中）。

### BranchSolve 真值（Phase-4.1 冻结）

| 指标 | 结果 |
|------|------|
| overall_accuracy | **1.00**（22 用例） |
| cross_case_frontier_crossing | **1.00** |
| refuse_case_honesty | **1.00** |

### LLM 种子注入（Phase-4.2 最终）

| 靶 | B(结构) | C(定向) | **D(+LLM)** | D−C |
|----|---------|---------|------------|-----|
| S2_api_path | 7 | 7 | **23** | **+16** |
| S1_role_chain | 12 | 12 | 12 | 0 |
| hash_gates | 41 | 41 | 65 | +24(归因 corpus policy) |

**S2 +16 是首个 LLM 反馈变异 > 结构反馈的真实覆盖证据。**

### RealWorld 端到端

| 臂 | 覆盖 |
|----|------|
| A(裸跑) | 33,562 exec/s |
| B/C/D | 95/657 块 (14.5%) |

---

## 冻结版本

| 标签 | 内容 |
|------|------|
| `phase1-freeze` | 静态/动态底座（21 真值满分） |
| `phase3-structural-freeze` | 前沿定向（10 runs 停止规则） |
| `phase4.2-llm-loop-complete` | LLM 覆盖循环（12/12 Mock E2E + 6 targets） |

---

## 技术栈

- **Fuzzing 引擎**：Atheris 3.1.0（libFuzzer for Python）
- **Oracle**：ANOTA 解释器（CPython 3.10.13 fork，NDSS'25）
- **静态分析**：Python AST + PyCG（ICSE'21）
- **LLM**：OpenAI 兼容 / Anthropic 双协议（GLM/Claude/local 可换）
- **测试框架**：pytest（255 tests 全绿）

## 依赖

```
atheris>=3.1
flask>=3.1
pytest>=8
networkx>=3.4
elementpath>=5
```

## License

Proprietary — Research use only.
