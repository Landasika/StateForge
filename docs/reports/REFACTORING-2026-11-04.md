# StateForge 模块重构交付记录（2026-11-04）

基线为 `0dbaf6d`。本次按照架构审查结果修改主代码、回归用例、发布配置及文档。最初的检查证据保留在 [审查报告](ARCHITECTURE-DECOUPLING-AUDIT-2026-11-04.md),该报告描述的是修改前状态。

## 模块边界与落地内容

| 范围 | 实际交付 | 兼容方式 |
|---|---|---|
| 数据契约 | C10 编解码下沉至 `core/seeds.py`;回放结果类型下沉至 `core/replay.py`;可选 IR 加载统一至 `core/serialization.py` | 旧入口导出保留;C10 字节生成、回放结果字段及 IR JSON 结构保持兼容 |
| 静态分析 | `extract_model` 单独提取事实,`analyze(..., annotator=...)` 组合标注;语义实现迁入 `analysis/semantics` | 原 `solving.llm` 模块路径指向同一实现对象,支持既有缓存 monkeypatch;6 份冻结缓存及提示词原样迁移 |
| 源码条件定位 | 公共 `SourceTestIndex` 只解析 IR 引用文件,按文件、词法作用域、行号建索引 | `_TestIndex` 别名保留;不带文件且存在歧义的查询返回 None |
| 执行服务 | 新增 `runtime.execution.execute_traced`,统一执行、观测、还原、异常记录与请求轨迹释放 | 保留各后端独立的评分、预算和判达结果类型;进程控制异常在清理后继续抛出 |
| 采集器 | CandidateCollector 与 FrontierCollector 继承公共 Collector,复用生命周期失败审计、状态账本和轨迹映射 | 保留 handle 返回值、反馈策略、语料元数据和回调;最近轨迹改从 `last_execution.events` 获取 |
| 搜索与计划执行 | 块定向 campaign、微靶计划执行器接入公共执行服务;驱动两份 handler 合并,在线 Session 反馈直接消费已映射结果 | 序列内状态连续性、输入间 reset/noop 语义、块覆盖判据保持原定义 |
| 规划策略 | 通用 PlanningPolicy 与显式 VulnshopPolicy 分开;公共 `render_plan_step`、`render_seed_step` 替代跨模块私有渲染引用 | GoalSpec 缺省 generic;VULNSHOP_GOALS 与历史商店桥显式选择 vulnshop;旧 `_to_step`、`_render_step` 保留别名/包装 |
| 语料发布 | SeedPublisher 统一 state/goal/LLM 候选的 spool/corpus 写入及内容去重 | 保留各自目录、内容 ID、origin、interesting_reason 和独立审计格式 |
| Web 适配 | 显式 app/client 构造,分开 Werkzeug 与 Django 传输;16 个 apps20 接口及 WSGI helper 迁移 | 保留已有输入协议与默认异常边界;可设置 suppress_errors=False 交由执行层记录异常 |
| 快照后端 | supervisor 接收 `--app-factory module:callable`,具体应用初始化移到 targets | 历史 `--target vulnshop/vampi` 由配置映射到工厂 |
| 图距离 | 普通目标距离与前沿 DistanceOracle 共享反向图和多源 BFS | 保留前沿版本缓存、最近前沿归属及不可达计数 |
| 实验配置 | 按 suite 集中不可变 ArmSpec,增加注册版本与规范策略说明 | D1/D2R/D2P 历史名称和参数继续可用;独立 `arm_registry.json` 记录配置,原汇总字段不变 |
| 配置与发布 | 主 Provider 接通 STATEFORGE_LLM_*;HTTP 依赖按需导入;基础/llm/replay/web/django/fuzz 依赖明确;wheel 包含提示词与冻结缓存,排除测试包 | legacy 环境变量及其超时默认值继续支持;增加 `make wheel-check`,接入 ci.sh |

## 已修复的审查问题

1. Flask 非 GET 请求改走 Werkzeug `open(path, method=..., json=...)`,POST/PUT/PATCH/DELETE 能实际进入目标视图。Django 靶函数不再用 lambda 替换客户端原生 generic 方法。
2. 同名、同行号条件不会跨文件覆盖;内层函数条件不会误归外层作用域。索引不再递归扫描 root 下的外部仓库或虚拟环境。
3. TraceSession 入栈保存真实的 `sys.gettrace()`,退出时正确恢复第三方 tracer 和嵌套同一 session 的外层 tracer。
4. 显式 selection_policy/energy_policy 优先于旧 scheduler_name 合并开关;CLI 缺省值不再伪装成显式策略。
5. 发布包包含语义提示词,基础安装不要求 Atheris/httpx/Flask 即可导入分析、种子和状态注入模块。
6. 文档中的 canonical LLM 配置进入实际 Provider。canonical key 配置时优先,否则保留 legacy 选择逻辑;canonical timeout 默认 30 秒,legacy 维持原实现 120 秒。
7. 两个 collector 消费后排空本次事件与请求索引;保留最近一次执行供在线反馈使用,避免随输入数保留全部已处理轨迹。
8. SSTG builder 只读取不变式的 one_way 属性,移除 Coupon.used 特例。

## 冗余清理

删除四个经审查确认无仓内消费者的文件:

- `analysis/auth_baseline.py`:废弃的空操作基线。
- `analysis/idioms.py`:未接入主链的旧实体字典。
- `solving/llm/real_provider.py`:与现用 Provider 并行且无人调用的旧实现。
- `adapters/realworld_generic.py`:未使用且根路径定位错误的旧入口。

同时删除 `_client_sig`、`_function_map`、`_match_source` 三个闲置/占位函数,收敛重复 IR 加载、生命周期处理、handler、请求渲染、种子落盘和 BFS。旧语义模块改为兼容别名,不是再保留一份实现。

研究工具、旧状态模型的有效消费者及相互独立的搜索算法仍保留。此次没有将覆盖代理当作运行时 verified 状态,没有把关系判达替换为块命中,也没有混合 Atheris 与 Python 搜索预算。

## 验证

- 新增 21 项专项缺陷回归覆盖真实 Flask 写请求、Django 传输协议、跨文件/作用域条件、嵌套 tracer、策略覆盖、轨迹释放、清理后重抛、canonical 配置、通用规划隔离、种子发布、应用工厂及可选依赖独立性。
- 架构门禁删除三条静态例外,新增核心管线禁止直接导入靶场、数据消费者禁止导入引擎入口的断言。适配器与评测入口仍可组装具体靶场。
- SF-Micro:21 个静态用例的 CFG/CALL/RETURN precision、recall 全部 1.00;5 个动态用例 trace accuracy 全部 1.00。
- Django:隔离环境的 Django 5.2.19 实测 GET/POST/PUT/PATCH/DELETE 请求体、认证头均通过。
- 冻结资源:6 份缓存与 1 份提示词逐字节比对基线,全部一致。
- wheel:从当前工作树临时副本构建,在全新虚拟环境只安装基础依赖后导入成功;包含提示词、6 份冻结缓存,无内置测试包;环境中没有 Atheris、httpx 或 Flask。
- 默认完整回归:904 passed、1 skipped、26 deselected、1 warning,耗时 619.02 秒;在屏蔽真实 LLM 配置的环境运行。相较基线 881 项通过,新增 21 项缺陷回归和 2 项架构门禁。唯一 warning 为既有 Pydantic PlanStep.json 名称遮蔽提示。
- `git diff --check` 与 `bash -n ci.sh` 通过。

未运行 ANOTA 专用用例、新的真实应用战役或在线 LLM 请求。默认测试中的真实 Atheris 子进程与小预算多臂实验属于回归验证范围。
