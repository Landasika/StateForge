**StateForge 模块解耦与主代码冗余检查（2026-11-04）**

检查基线：`0dbaf6d`。本次新增检查报告，未修改、移动或删除主代码。

结论：项目已经有明确的数据契约和目录分层，结构分析底座的职责比较集中；主要维护负担来自多阶段功能并存后的执行流程重复、状态表示兼容、实验策略别名，以及部分底层模块对入口和具体靶场的依赖。可直接清理的闲置代码数量有限，主要收益应来自职责收敛，而不是大规模删文件。

**检查范围与统计口径**

对 `stateforge/` 主代码做 AST 导入和符号引用检查，追踪 CLI、Atheris 驱动、目标适配器、求解注入器、Session 搜索与评测入口；结合测试、README、架构与协议文档核实用途。对关键问题执行独立最小复现，并从当前提交的临时副本构建 wheel 检查实际发布内容。

统计排除 `targets/`、`fixtures/`、`tests/`、缓存、`external/` 和 `anota/`。行数包含空行、注释与文档字符串，不等同于有效代码行。

| 模块 | Python 文件数 | 行数 | 当前职责 |
|---|---:|---:|---|
| `core/` | 5 | 308 | ProgramIR、AppModel、计划数据、序列化与日志 |
| `analysis/` | 18 | 2,865 | CFG/ICFG、框架端点与语义事实提取、SSTG |
| `state/` | 33 | 7,442 | 状态事实、Session 访问、状态目标、观测证据与规划 |
| `runtime/` | 17 | 2,392 | 追踪、映射、生命周期、执行、回放与快照 |
| `adapters/` | 8 | 279 | bytes 与 Flask/Django/WSGI/可调用目标之间的适配 |
| `fuzzing/` | 32 | 4,017 | Atheris 接线、结构反馈、Session 调度、定向搜索 |
| `solving/` | 33 | 5,107 | 分支求解、LLM、状态／关系计划、检查点与种子注入 |
| `evaluation/` | 31 | 6,679 | 基准、实验编排、统计、冻结与报告 |
| 包根入口／配置 | 3 | 442 | CLI、配置、包初始化 |
| 合计 | 180 | 29,531 | 含评测，不含目标、夹具与测试 |

进一步排除 `evaluation/` 后，算法与运行代码为 149 个 Python 文件、22,852 行。`state/` 占上述 29,531 行的 25.2%，`evaluation/` 占 22.6%；这两个目录最值得继续细化职责。

**实际运行主线**

1. 结构底座：`analysis/static/icfg.py` 构造 ProgramIR；`runtime/tracer.py` 收集轨迹；`runtime/mapper.py` 映射块／边；`fuzzing/feedback/` 计算新颖度、前沿、距离与语料价值。
2. Atheris 实验线：`evaluation/experiment/epoch.py` 启动 `fuzzing/entry_driver.py`；采集器执行输入并保留种子；`solving/` 注入器在轮间生成补充种子。
3. Session 线：`state/session_access.py` 与 `session_index.py` 给出访问点与路径条件；`session_guided.py` 做启动前搜索，`session_online.py` 在 Atheris 运行中参与变异；`solving/session_replay.py` 验证并复用准备前缀。
4. 新增块定向线：`fuzzing/guidance/` 自己组织选种、能量分配、BasicMutator、目标执行、语料保留与证据输出。这是独立的 Python 搜索循环，不能直接当作已接入 Atheris 的同一调度器。

这几条线共享静态图和轨迹映射，但执行与生命周期的组织尚未统一。各条线支持不同的实验问题，不能仅因名称接近就互相替代。

**按模块的解耦判断与建议**

| 模块 | 当前判断与具体证据 | 建议边界 |
|---|---|---|
| `core/` | 内部依赖比较干净，是稳定底座；但 C10 序列编码仍在 Atheris 入口中 | 将纯种子编解码放入低层公共契约模块；保留旧入口导出以兼容调用方 |
| `analysis/` | `analyzer.py:34` 直接导入 solving 的 LLM 标注和 Provider，并自行读取配置；`builder.py:93` 识别 `Coupon.used` | 源码提取与语义提升分开；由上层注入标注器；图构建只读取模型中的单向属性 |
| `state/` | 同时承载静态提取、旧抽象状态、新 Session 事件、关系语义、运行时证据、规划及靶场 checker | 明确静态事实、证据契约、目标推导、规划四个子域；先建立转换接口，再迁移旧模型消费者 |
| `runtime/` | 已有 ExecutionContext 与生命周期协议，但各执行器、采集器和搜索器自行拼接 reset、trace、map、observe；快照 supervisor 直接选择具体靶场 | 统一单次／逐步执行服务，按需组合生命周期、观测与计量；应用工厂由入口注入 |
| `adapters/` | TargetAdapter 仅声明 prepare/execute；真实应用多处通过 `__new__` 注入 client，并改写客户端方法；通用适配器混合输入解析、框架传输和错误吞并 | 支持显式 client 构造；框架传输分别实现；由执行边界统一记录异常与失败类型 |
| `fuzzing/` | 两个 collector 的执行前半段几乎同形；entry_driver.main 约 298 行，混合参数、加载、生命周期、调度和落盘 | 把入口缩成组装层；执行／观测公共流程与反馈／保留策略分开组合 |
| `solving/` | 约束、目标、关系、Session 等多种求解器并存，属于不同问题；但多个模块直接写语料、账本，并导入其他模块私有函数 | 求解器返回候选种子／计划和证据；公共注入服务负责去重、语料写入和审计 |
| `evaluation/` | 实验、真实应用启动、冻结、聚合报告集中在一个域；`four_arm.py` 和 runner 重复声明策略，且同名臂的含义跨入口不同 | 用一份有版本的实验配置描述 mode、反馈、注入策略和预算；各入口只选择配置与生成报告 |
| 配置／发布 | 配置有两套环境变量入口，实际主 Provider 使用 legacy 族；运行依赖多数放在 test extra；wheel 包含测试与靶场，却漏提示词 | 先统一配置解析并保留环境变量别名；按结构／语义／Web／fuzz 能力声明依赖，增加 wheel 安装验证 |

**最值得先切断的依赖**

`entry_driver.py:15` 顶层导入 Atheris，但纯粹的 `encode_sequence_seed`／`decode_sequence_seed` 定义在同一文件。`goal_checkpoint.py`、`state_seed_injector.py`、`session_replay.py`、`session_guided.py`、`session_online.py` 等因此为序列化数据依赖整个驱动。模拟环境缺少 Atheris 时，导入 `goal_checkpoint`、`state_seed_injector`、`session_online` 均失败。

建议先把 C10 编解码移到公共低层模块，再将 Atheris 导入放入驱动执行入口。这样既减轻可选依赖，也切开 `entry_driver → session_guided/session_online → entry_driver` 的调用时回路。后者有函数内延迟导入，当前不等于必然发生循环导入错误，但模块关系仍不适合独立使用。

`state/planning/checkers.py:11` 对 `runtime.replay.GoalResult` 是无条件导入。`from __future__ import annotations` 不会阻止该 import 执行，架构文档称其“零运行耦合”不准确。应改为 TYPE_CHECKING 引用，或把结果契约下沉。它与 replay 中的延迟 checker 导入组成调用时回路。

`runtime/snapshot/supervisor.py:39` 按靶名导入 vulnshop 或 VAmPI；`state/planning/compat.py`、`goals.py`、`checkers.py` 包含专属路由、JSON 参数与业务判据。通用规划器目前无条件使用部分兼容规则，应改为调用方显式选择策略。仅移动文件，而仍默认调用这些规则，不能完成解耦。

`solving/relational_planner.py:32` 导入 `vulnshop_state_bridge._render_step`，并引用 `backward._to_step`。通用请求渲染应成为公共接口，靶场参数填充应留在具体适配策略中。

**高置信度的清理候选**

以下判断基于当前仓库的 Python 导入、符号引用、动态路径字符串和文档检索；没有据此断言仓库外一定没有使用者。本次未执行删除。

| 文件／符号 | 规模 | 当前证据 | 处置建议 |
|---|---:|---|---|
| `analysis/auth_baseline.py` | 15 行 | 文件明确标记废弃；函数只返回 0；无仓内调用方 | 删除废弃空壳，必要时单独保留兼容导出 |
| `analysis/idioms.py` | 49 行 | load、row_entity、single_use_attrs 无仓内消费者；旧实体／认证名字典已脱离当前提取链 | 删除旧字典与加载逻辑 |
| `solving/llm/real_provider.py` | 89 行 | RealLLMProvider、MockLLMProvider 无代码调用方；当前 analyzer 与 solver 使用 provider.py 协议 | 统一配置后删除旧实现，测试 mock 放测试支持代码中 |
| `adapters/realworld_generic.py` | 29 行 | 无仓内调用方；包含应用启动副作用；parents[3] 指向工作区上级而非仓库根 | 删除旧入口，或迁移为显式选择的目标插件 |
| `adapters/generic_web.py::_client_sig` | 7 行 | 无调用方；当前用 open 属性分派 | 删除闲置探测函数 |
| `solving/guard_recovery.py::_function_map` | 18 行 | 无调用方；输出构造也未完成，当前使用 _TestIndex | 删除未完成旧实现 |
| `solving/input_provenance.py::_match_source` | 11 行 | 无调用方；占位逻辑始终返回 None；实际读取使用 _extract | 删除占位函数 |
| 合计 | 218 行 | 4 个文件和 3 个私有函数，包含注释及空行 | 可作为第一批小规模清理 |

此外，analyzer 中的 `label_rule_based` 导入、snapshot_replay 中的 `_safe_json` 导入、goal_to_plan 中的 `_store_table` 导入等没有被使用。这类 import 清理收益较小，应作为最后的机械整理；TYPE_CHECKING 或字符串类型注解中的名称不能按简单 AST 名称计数删除。

**应抽公共流程的重复实现**

两个 collector 的 `_lifecycle_failed` 为相同实现；handle 中的执行、生命周期处理、状态观测和轨迹映射也高度相似。entry_driver 的两个 handler 为相同的 10 行实现。适合抽出执行公共流程和 handler 工厂，保留反馈 observe/commit 的差异。

`state_seed_injector` 和 `vulnshop_state_bridge` 的 `_coerce_ir` 为相同实现，可以收口到公共 IR 加载入口。`feedback/distance.py` 与 `distance_oracle.py` 都做反向 BFS，但后者有前沿版本缓存与最近前沿归属，适合共享图算法，保留各自目标与缓存语义。

`state_seed_injector.py`、`goal_to_plan.py`、`goal_app_bridge.py`、`relational_planner.py` 分别处理不同目标和应用模型。应抽共享事实查询、AppModel 构造、请求渲染和计划持久化；不应直接将四种策略压成一个包含更多分支的大函数。

**功能已经相同的历史实验臂**

`evaluation/experiment/runner.py:59` 的 D1 与 D2R 均采用 `mode=frontier`、`use_feedback=True`、`policy=reinjection`。D2P 传入 portfolio，但 `epoch.py:304` 明确说明 portfolio 与 reinjection 已等价，当前循环也没有执行原来的 focused 行为。

因此，三者当前搜索策略等价，差异主要是记录标签和独立运行噪声。历史名称可保留作旧产物读取别名；新实验没有必要把它们解释成三种不同算法。保留历史复现时，应同时记录代码版本，而不是只记录组名。

**应隔离或归档、尚不能直接判死的模块**

| 模块／分支 | 仓内使用现状 | 建议 |
|---|---|---|
| `analysis/cfg.py` | build_cfg 已委托 `analysis/static/cfg.py`；负责 NetworkX 投影、状态锚点、XFG 和数据流；主要由测试消费 | 属兼容／分析工具层，保留或拆出工具，不按重复 CFG 算法删除 |
| `analysis/svg.py` | 无 Python 调用方，但提供额外图渲染能力 | 有保留需要则放评测可视化或工具入口 |
| `analysis/pycg_bridge.py` | 无 Python 调用方；CLI 支持读取外部 callgraph，但没有接它生成调用图 | 明确接 CLI 或归入可选工具；未接线不等于算法无用 |
| `solving/llm/annotate.py` | 无 Python 调用方；实际为确定性的 ANOTA 注解草稿工具，含靶场假设 | 移出通用 LLM 求解主链，归入工具／历史实验 |
| `state/annotation/annotator.py` | LLMRepositoryAnnotator 无调用方；model/mock/validator/index 分支以测试使用为主 | 明确作为可选能力，或归档研究原型，避免与当前 lifting 通道混为一谈 |
| `solving/stagnation.py`、`plateau.py` | 组合检测器由测试消费；在线轮间主线使用 StallTracker | 保留实验版本或归档；执行次数窗口与 epoch 窗口语义不同，不能直接互换 |
| `runtime/replay.py`、`snapshot_replay.py` | 主要由规划／快照测试消费，缺少当前 CLI 主链接线 | 明确公开回放能力范围，整理为可选执行后端 |
| `analysis/static/predicate.py`、`fuzzing/guidance/deviation.py` | 最近合入的新能力；当前主要由对应测试消费 | 作为后续分支定向能力的基础保留，不以暂未接入 campaign 为由删除 |
| `fuzzing/harness.py` | README 明确提供 run_one 公共 Python 集成用法 | 公开入口，不能仅因仓内无调用而删除 |
| `state/session_access.py` 的 legacy 分支 | observer、transition、state_seed_injector 仍在使用 | 先迁移消费者与兼容测试，再收缩旧实现 |

**已复现的功能问题与影响**

| 优先级 | 位置 | 复现结果 | 影响与建议 |
|---|---|---|---|
| P1 | `adapters/generic_web.py:63` | Flask 3.1.0 客户端没有 request 方法；同一测试应用 GET 返回 200，POST/PUT/DELETE 均返回 None，处理函数调用次数保持不变 | 写请求未进入目标，异常又被适配器吞掉；修正为对应客户端传输接口，并区分协议错误与目标异常 |
| P1 | `solving/guard_recovery.py:41` | 两文件都有 line 2 的 gate；a.py 条件 x==1、b.py 条件 x==2，二者均恢复成 x==2 | 索引只有函数名和行号，跨文件覆盖；应使用文件、函数限定名和源码位置键，统一源码索引入口 |
| P1 | `runtime/tracer.py:89` | 进入 TraceSession 前安装 tracer，退出后未恢复；同一 session 嵌套 with 后，外层 tracer 也未保留 | 保存的是旧 self._previous 而非入栈时 sys.gettrace；影响嵌套追踪与其他追踪工具共存 |
| P1 | `fuzzing/guidance/campaign.py:88`、`cli.py:181` | 传 scheduler_name=guided、selection_policy=uniform、energy_policy=fixed 后，实际变成 distance/distance | 旧合并开关覆盖新拆分配置，与注释及 CLI override 帮助相反；影响选种／能量消融的解释 |
| P1 | `pyproject.toml:41`、`solving/llm/lifting.py:26` | wheel 构建成功，但不含 prompts/v1.md；解包后导入 lifting 触发 FileNotFoundError | 源码 editable 安装通过不能证明发布包完整；将提示词纳入 package-data 并验证 wheel 安装 |
| P2 | `config.py`、`solving/llm/provider.py:34` | 仅设置 README 主配置族 STATEFORGE_LLM_API_KEY 时，llm_settings 读取成功，实际 Provider.from_env 返回 None | 配置解析与实际主链脱节；统一 Provider 读取路径并兼容旧环境变量 |
| P2 | `fuzzing/candidates.py:65`、`frontier_collector.py:54` | 旧 collector 处理 10 个简单输入后保留 40 个事件，20 个输入后保留 80 个事件；request 索引也累积 | 已处理轨迹随输入数线性增长；建议执行后排空或采用有界缓冲。未据此声称已测得生产负载下 OOM |

这些复现是在临时夹具／副本中完成，未调用真实 LLM，也未重新开展真实应用战役。单元与集成测试的通过范围需要与这些额外场景分别解释。

**建议的收敛顺序**

1. 先修已复现的请求执行、守卫定位、追踪恢复、配置覆盖和 wheel 缺文件问题。这些会影响功能或实验口径，优先级高于删旧文件。
2. 下沉 C10 编解码，修正 checkers 的类型导入，引入明确的 client 构造和应用工厂接缝。保留已有序列字节契约与旧导出路径。
3. 清理 218 行高置信度候选，清理前确认是否还需要仓外兼容；随后整理闲置导入。
4. 抽出公共执行／观测／轨迹消费流程，让两个 collector、Session 搜索、块定向 campaign 和计划执行器逐步接入。保留各后端的预算、反馈和判据差异。
5. 给静态事实、目标推导、规划和证据判定建立公共 API，收敛私有函数跨模块引用；通过适配层逐个迁移旧状态消费者。
6. 收敛实验配置与策略别名，归档历史工具，按可选能力整理安装依赖与发布内容。

结构冻结契约、覆盖代理与运行时 verified 证据、关系判达与目标块命中、Atheris 与 Python 定向搜索预算，均应继续保持各自定义。重构时共享执行设施，并不意味着这些研究口径可以合并。

**验证记录**

- SF-Micro：21 个静态用例的 CFG/CALL/RETURN precision 与 recall 全为 1.00；5 个动态用例的 trace accuracy 全为 1.00。
- 默认 pytest 门禁：881 passed、1 skipped、26 deselected；耗时 757.62 秒。运行时清除了两套 Provider 使用的真实 LLM 凭据，以验证离线回归路径；保留测试内部的模拟配置。
- wheel：在当前提交的临时副本构建成功；验证了缺提示词导致的导入失败；实际包内有 297 个 targets 路径条目、146 个 tests 路径条目。
- 独立复现：Flask 非 GET 请求、跨文件守卫索引、追踪器恢复、调度配置优先级、LLM 配置断路、可选 Atheris 依赖及轨迹缓存增长。
- 未执行 ANOTA 专用测试或新的真实应用／LLM 战役；未修改主代码或现有测试断言。
