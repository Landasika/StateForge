# StateForge 架构分层

**九域分层 + 例外登记制:依赖只允许向下,例外必须登记。** 本文是分层规范的规范文,机器执行面在 `stateforge/tests/test_architecture_boundaries.py`(pytest 即测)。两者经**例外条数互锁**绑定:下表行数恒等于测试内 `len(ALLOWED_EXCEPTIONS) + 2`,改一边必须同步另一边,文档与测试不会漂移。

---

## 分层图

```text
┌────────────────────────────────────────────────────────────┐
│ L4    evaluation(评测域)          cli(命令行入口)           │  顶层入口
└─────────────────────────────┬──────────────────────────────┘
                              ▼
┌────────────────────────────────────────────────────────────┐
│ L3    solving(求解域:Guard→距离→溯源→约束→LLM 种子注入)      │
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
| L1.5 | `stateforge/state/` | 状态域:状态提取与语义模型、会话访问/状态溯源、标注子包、规划子包 |
| L2 | `stateforge/runtime/` | 运行时域:sys.settrace 轨迹采集、轨迹→ICFG 映射、覆盖度量、回放与快照子包 |
| L2.5 | `stateforge/fuzzing/` | Fuzzing 集成域:Atheris 子进程入口、选择性插桩、语料/候选/前沿、结构反馈子包 |
| L2.5 | `stateforge/adapters/` | 目标适配层:TargetAdapter 协议 + 通用 Web/Flask/可调用/vulnshop 适配器 |
| L3 | `stateforge/solving/` | 求解域:Guard 恢复→操作数→距离→溯源→约束→变异提示→停滞触发→LLM 种子注入 |
| L4 | `stateforge/evaluation/` | 评测域:SF-Micro 真值基准、BranchSolve 报告、Benchmark 基线、实验与可视化子包 |
| L4 | `stateforge/cli.py` | 命令行入口:analyze / feedback / experiment / viz |

**域外(不入分层):** `stateforge/config.py` 是配置叶(importer 为 `solving/llm/real_provider.py` 与 `solving/llm/provider.py`);`stateforge/targets/`(靶场)与 `stateforge/fixtures/`(真值数据)是数据域,不是可依赖层;`stateforge/tests/` 可任意引用,均不在边界扫描范围。

---

## 分层规则

1. **只允许向下引用**:任何模块只能 import 严格更低层(数值更小)的域,以及**自域**(含子包)。
2. **同层仅允许两个有向对**:同层跨域引用默认违规,白名单只有 adapters→fuzzing(适配器取 `fuzzing.instrumentation` 插桩器)与 cli→evaluation(viz 子命令);反向(fuzzing→adapters、evaluation→cli 静态边)已在 P2-T1 解环清除,重新引入须按例外登记。
3. **向上引用 = 违规**:边界测试对"高层 → 严格更低层"以外的一切跨域引用报错,豁免=下方例外登记表与规则 2 的两个同层有向对。
4. **动态边静态不可见**:subprocess `-m`、`__import__` 等运行时边不入静态扫描,但必须在例外表登记留痕(下表第 4 行)。
5. **例外条数互锁**:例外登记表数据行数 == 测试内 `len(ALLOWED_EXCEPTIONS) + 2`(第 4、5 行为静态扫描不可见的留痕行,只入表、不入测试例外集)。

---

## 例外登记表

| 来源文件 | 指向 | 理由 |
|---|---|---|
| stateforge/analysis/analyzer.py | solving | 标注边界:analyzer 编排 rule-based/LLM 双协议语义标注(:17 heuristic、:34 lifting、:35 provider 全走 solving.llm),属有意设计而非层次泄漏;搬移风险大于收益(P2 裁定) |
| stateforge/state/planning/checkers.py | stateforge.runtime.replay | 仅类型回边:GoalResult 只用于 checker 签名注解(:11),`from __future__ import annotations` 下零运行耦合,无导入副作用 |
| stateforge/runtime/snapshot/supervisor.py | targets | 靶场硬编码:build_app 按 args.target 直引 targets.vulnshop.app / targets.vampi.adapter(:42、:47);runtime 不该认得具体靶场,P3 注入候选(改为调用方注入 app 工厂) |
| stateforge/evaluation/experiment/four_arm.py:93、stateforge/evaluation/experiment/epoch.py:30-31、stateforge/evaluation/benchmark_baseline.py:96 | cli / fuzzing.entry_driver / runtime.coverage | 运行时边,静态不可见:subprocess `-m stateforge.cli`、subprocess `-m stateforge.fuzzing.entry_driver`、`__import__("stateforge.runtime.coverage")`;仅入表留痕,不入测试例外集 |
| stateforge/targets/vulnshop/serve.py:11、stateforge/targets/vampi/adapter.py:20 | stateforge.targets.*(域内自引用) | targets 胶合文件自引用:serve 直引同域 adapter/app,vampi 适配器复用 vulnshop 适配器的 TargetSpec;targets/ 为数据域不在扫描范围,仅入表留痕 |

**登记流程:** 出现新的跨域引用时,先问能否改 import 消除;确需保留,则在本表追加一行**并**在 `test_architecture_boundaries.py` 的 `ALLOWED_EXCEPTIONS` 追加对应元组(条数互锁会强制两边同步)。例外表只增不删时须在理由列写明裁定出处与回退条件。

---

## 执行面

```bash
# 仅跑边界测试
python -m pytest stateforge/tests/test_architecture_boundaries.py -v

# 四条断言:core 纯净 / 无未登记向上引用 / 依赖汇(evaluation、adapters)零管线入边 / 例外条数互锁
```

边界测试扫描 `stateforge/**/*.py`(排除 targets/、fixtures/、tests/),AST 级提取 `from stateforge.X ...` / `import stateforge.X`;动态边与 targets 域内自引用因此天然不可见,以例外表第 4、5 行留痕。
