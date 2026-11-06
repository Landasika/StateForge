# StateForge 架构分层

**十域分层 + 例外登记制:依赖只允许向下,例外必须登记。** 本文是分层规范的规范文,机器执行面在 `stateforge/tests/test_architecture_boundaries.py`(pytest 即测)。两者经**例外条数互锁**绑定:下表行数恒等于测试内 `len(ALLOWED_EXCEPTIONS) + DYNAMIC_EXCEPTION_ROWS`,改一边必须同步另一边,文档与测试不会漂移。

---

## 分层图

```text
┌────────────────────────────────────────────────────────────┐
│ L4    evaluation(评测域)          cli(命令行入口)           │  顶层入口
└─────────────────────────────┬──────────────────────────────┘
                              ▼
┌────────────────────────────────────────────────────────────┐
│ L3.5  pipeline(GoalRunner:分析→规划→执行→验证)             │
└─────────────────────────────┬──────────────────────────────┘
                              ▼
┌────────────────────────────────────────────────────────────┐
│ L3    solving(求解域:Guard→距离→溯源→约束→种子注入)          │
└─────────────────────────────┬──────────────────────────────┘
                              ▼
┌────────────────────────────────────────────────────────────┐
│ L2.5  fuzzing(Atheris 集成/结构反馈) ← adapters(目标适配)  │  同层有向对
│       (adapters→fuzzing 单向放行,反向违规)                  │
└─────────────────────────────┬──────────────────────────────┘
                              ▼
┌────────────────────────────────────────────────────────────┐
│ L2    runtime(运行时:轨迹采集/ICFG 映射/回放/快照)           │
└─────────────────────────────┬──────────────────────────────┘
                              ▼
┌────────────────────────────────────────────────────────────┐
│ L1.5  state(状态域:提取/语义模型/标注/规划)                  │
└─────────────────────────────┬──────────────────────────────┘
                              ▼
┌────────────────────────────────────────────────────────────┐
│ L1    analysis(静态分析:CFG/ICFG/语义分析编排/图导出)        │
└─────────────────────────────┬──────────────────────────────┘
                              ▼
┌────────────────────────────────────────────────────────────┐
│ L0    core(数据契约:ProgramIR/AppModel/JSON 序列化)          │
└────────────────────────────────────────────────────────────┘
```

---

## 每域一句职责

| 层 | 域 | 一句职责 |
|----|----|---------|
| L0 | `stateforge/core/` | 数据契约:ProgramIR(结构 IR)+ AppModel(语义 IR)+ JSON 序列化,不引用任何内部域 |
| L1 | `stateforge/analysis/` | 静态分析域:单函数 CFG 与全程序 ICFG、语义分析入口与模式匹配、SSTG 图构建导出 |
| L1.5 | `stateforge/state/` | 状态域:状态提取与语义模型、会话访问/状态溯源、运行时观测契约(v2 结构化列+属性级可观测性)、标注子包、规划子包;关系语义工作线(relational.py 关系 IR/provenance.py 变量来源/goal_evaluation.py 联合求值) |
| L2 | `stateforge/runtime/` | 运行时域:sys.settrace 轨迹采集、轨迹→ICFG 映射、覆盖度量、输入级执行上下文(生命周期+观测编排)、回放与快照子包(l2_sqlite backup/copy 双捕获) |
| L2.5 | `stateforge/fuzzing/` | Fuzzing 集成域:Atheris 子进程入口、选择性插桩、语料/候选/前沿、结构反馈子包 |
| L2.5 | `stateforge/adapters/` | 目标适配层:TargetAdapter 协议 + 通用 Web/Flask/可调用/vulnshop 适配器 |
| L3 | `stateforge/solving/` | 求解域:Guard 恢复→操作数→距离→溯源→约束→变异提示→停滞触发→种子注入(状态序列种子双路径:微靶 M1/M2 合成 / AppModel 真路径 `vulnshop_state_bridge`,P4-T3;LLM 种子注入) |
| L3.5 | `stateforge/pipeline/` | 主流程协调与可审计产物;复用分析、状态规划、运行时和求解模块,不实现规划算法 |
| L4 | `stateforge/evaluation/` | 评测域:SF-Micro 真值基准、BranchSolve 报告、Benchmark 基线、实验与可视化子包 |
| L4 | `stateforge/cli.py` | 命令行入口:analyze / feedback / experiment / viz / state / guided / goal-run |

**域外(不入分层):** `stateforge/config.py` 是配置叶,统一 LLM 环境变量及历史靶名到工厂路径的映射;`stateforge/targets/`(靶场)与 `stateforge/fixtures/`(真值数据)是数据域,不是可依赖层;`stateforge/tests/` 可任意引用,均不在边界扫描范围。

---

## 分层规则

1. **只允许向下引用**:任何模块只能 import 严格更低层(数值更小)的域,以及**自域**(含子包)。
2. **同层仅允许两个有向对**:同层跨域引用默认违规,白名单只有 adapters→fuzzing(适配器取 `fuzzing.instrumentation` 插桩器)与 cli→evaluation(viz 子命令);反向(fuzzing→adapters、evaluation→cli 静态边)已在 P2-T1 解环清除,重新引入须按例外登记。
3. **向上引用 = 违规**:边界测试对"高层 → 严格更低层"以外的一切跨域引用报错,豁免=下方例外登记表与规则 2 的两个同层有向对。
4. **动态边静态不可见**:subprocess `-m`、`__import__` 等运行时边不入静态扫描,但必须在例外表登记留痕(下表动态登记行)。
5. **例外条数互锁**:例外登记表数据行数 == 测试内 `len(ALLOWED_EXCEPTIONS) + DYNAMIC_EXCEPTION_ROWS`(动态登记行为静态扫描不可见的留痕行,只入表、不入测试例外集)。

---

## 例外登记表

| 来源文件 | 指向 | 理由 |
|---|---|---|
| stateforge/pipeline/goal_fuzz.py:fuzz_checkpoint | fuzzing.entry_driver | 主流程以 subprocess 启动真实 Atheris;显式使用 reset 生命周期,按精确种子哈希核对输入执行回执 |
| stateforge/solving/checkpoint_replay.py:cold_replay | solving.checkpoint_replay | 独立解释器重放候选检查点;同域动态子进程边,不规划,仅依据真实轨迹报告冷启动验证 |
| stateforge/evaluation/experiment/four_arm.py:93、stateforge/evaluation/experiment/epoch.py:30-31、stateforge/evaluation/benchmark_baseline.py:96 | cli / fuzzing.entry_driver / runtime.coverage | 运行时边,静态不可见:subprocess `-m stateforge.cli`、subprocess `-m stateforge.fuzzing.entry_driver`、`__import__("stateforge.runtime.coverage")`;仅入表留痕,不入测试例外集 |
| stateforge/config.py:SNAPSHOT_APP_FACTORIES | stateforge.targets.snapshot_factories | 配置中的历史靶名映射;runtime 仅加载注入的 module:callable,支持 --app-factory 自定义工厂 |
| stateforge/targets/vulnshop/serve.py:11、stateforge/targets/vampi/adapter.py:20 | stateforge.targets.*(域内自引用) | targets 胶合文件自引用:serve 直引同域 adapter/app,vampi 适配器复用 vulnshop 适配器的 TargetSpec;targets/ 为数据域不在扫描范围,仅入表留痕 |

**登记流程:** 出现新的跨域引用时,先问能否改 import 消除;确需保留,则在本表追加一行**并**在 `test_architecture_boundaries.py` 的 `ALLOWED_EXCEPTIONS` 追加对应元组(条数互锁会强制两边同步)。例外表只增不删时须在理由列写明裁定出处与回退条件。

---

## 执行面

```bash
# 仅跑边界测试
python -m pytest stateforge/tests/test_architecture_boundaries.py -v

# 四条断言:core 纯净 / 无未登记向上引用 / 依赖汇(evaluation、adapters)零管线入边 / 例外条数互锁
```

边界测试扫描 `stateforge/**/*.py`(排除 targets/、fixtures/、tests/),AST 级提取 `from stateforge.X ...` / `import stateforge.X`;动态边与 targets 域内自引用因此天然不可见,以例外表动态登记行留痕。

## 2026-11-04 重构后的公共边界

- `core.seeds` 承载 C10 编解码;`core.replay` 承载回放结果。数据消费者不导入 Atheris 入口。
- `analysis.analyzer.extract_model` 只提取源码事实;`analyze(..., annotator=...)` 组合语义后端与不变式推导。语义实现位于 `analysis.semantics`,旧 `solving.llm` 路径保留模块别名,缓存键与提示词版本不变。
- `analysis.source_index.SourceTestIndex` 仅解析 IR 引用的文件,按文件、词法作用域与行号定位条件。模糊的旧式查询返回 None。
- `runtime.execution.execute_traced` 负责输入执行、生命周期、观测前还原顺序及轨迹释放。两个 collector 共享 `fuzzing.collector.Collector`,各自保留评分与提交策略;最近一次轨迹在 `last_execution.events`,映射结果在 `last_mapped`。
- `state.planning.policies` 显式选择 generic/vulnshop 策略;`GoalSpec.profile` 缺省 generic。VULNSHOP_GOALS 与历史商店桥显式启用 vulnshop。公共请求渲染位于 `state.planning.rendering`。
- `fuzzing.seed_publisher.SeedPublisher` 统一候选种子的 spool/corpus 落盘与内容去重,求解器保留各自审计与判据。
- `runtime.snapshot.supervisor` 接收 `--app-factory module:callable`;历史 `--target` 经配置映射兼容,具体应用初始化位于 targets。
- `evaluation.experiment.arms` 是实验臂定义源,按 suite 区分 B/C 标签含义,在独立 arm_registry.json 中记录注册版本及 canonical_policy。D1/D2R/D2P 保留历史名称与调用参数,当前规范策略均为 reinjection。

本次消除了三条静态例外,新增门禁禁止管线直接导入靶场、禁止种子消费者导入引擎入口。研究判据、轨迹格式、C10 字节编码及各搜索后端预算口径继续分别定义。
