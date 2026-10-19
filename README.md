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
- [配置](#配置)
- [项目阶段](#项目阶段)
- [模块详解](#模块详解)
- [实验结果](#实验结果)
- [冻结版本](#冻结版本)

---

## 快速开始

```bash
# 安装（唯一依赖源：pyproject.toml；atheris 无预编译 wheel 时 sdist 编译需 clang）
pip install -e ".[dev,test,fuzz]"

# 跑全量测试（计数随阶段增长，以"冻结版本"节为准；26 个 anota 标记用例默认 deselect、在 ANOTA 解释器内另跑）
pytest

# 本地 CI：make test 等价 pytest；make verify = ./ci.sh 完整验收链（临时 clone→venv→install→pytest→micro_benchmark，输出三验收数字）
make test
make verify

# 静态分析：构建 ICFG
python -m stateforge.cli analyze <目标目录> --out results/structural

# 状态发现（M2 迁移发现：session 变量/谓词/域）
python -m stateforge.cli state <目标目录> --out results/state.json

# 运行时反馈：执行轨迹 → 覆盖/前沿
python -m stateforge.cli feedback results/structural/program_ir.json trace.jsonl --out feedback.json

# 可视化：静态+动态叠图
python -m stateforge.cli viz program_ir.json feedback.json --out coverage.dot
# 可视化叠图（dot 命令可选，需系统 graphviz）
dot -Tsvg coverage.dot -o coverage.svg

# 微基准验证（21 用例，全部 1.00）
python -m stateforge.evaluation.micro_benchmark

# 五臂实验（A/B/C/D/E 对照，E=状态臂）
python -m stateforge.evaluation.experiment.four_arm --out results/experiment --runs 3 --epochs 3 --seconds 8

# 五臂战役（E=状态臂；--groups 可只跑子集臂，如正式口径 "D1,E"；ir 须为套件目录 analyze 的 program_ir.json）
python -m stateforge.evaluation.experiment.campaign <target.py> results/structural/program_ir.json --out results/experiment/campaign --runs 10 --epochs 3 --seconds 10 --groups D1,E
```

---

## 配置

LLM 语义求解(Phase 4.2)经 4 个环境变量配置;`stateforge/config.py` 是唯一读取入口(`from stateforge.config import llm_settings`),代码侧不散读 `os.environ`。

| 变量 | 默认 | 用途 |
|------|------|------|
| `STATEFORGE_LLM_API_KEY` | `""`(空串) | API 密钥;未配置时 `configured=False`,LLM 求解自动降级规则标注,链路不阻塞 |
| `STATEFORGE_LLM_BASE_URL` | `https://open.bigmodel.cn/api/paas/v4` | OpenAI 兼容端点(尾部 `/` 自动去除) |
| `STATEFORGE_LLM_MODEL` | `glm-4-flash` | 模型名 |
| `STATEFORGE_LLM_TIMEOUT` | `30` | 请求超时(秒;按 int 收敛,小数字符串报错) |

**Anthropic 代理形态**(2026-10-15 实测,glm-5.3-flash):设
`ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL`(Bearer 代理)即可,
`SF_LLM_MODEL` 指定模型;自签证书代理加 `SF_LLM_SSL_VERIFY=false`。
与 OpenAI 兼容形态二选一,Provider 自动识别(优先级见
`solving/llm/provider.py` 模块头)。

日志:库侧诊断走 `logging` 输出到 stderr(`stateforge.core.log` 统一入口 `get_logger`;默认 INFO,静默直到 CLI 调 `setup_logging`)。`python -m stateforge.cli -v/--verbose` 开 DEBUG;CLI 用户面向输出(命令结果/表格)仍走 stdout print,两者不混流。

---

## 项目阶段

| 阶段 | 内容 | 状态 | 标签 |
|------|------|------|------|
| **Phase 1** | 静态/动态底座（ICFG + Trace + Mapper） | ✅ 冻结 | `phase1-freeze` |
| **Phase 2** | 结构新颖度反馈闭环 | ✅ 完成（零增益实证） | |
| **Phase 3** | 前沿定向反馈（距离+组合+预算） | ✅ 冻结（停止规则成立） | `phase3-structural-freeze` |
| **Phase 4.1** | 确定性分支求解 | ✅ 冻结（22/22 用例 100%） | |
| **Phase 4.2** | LLM 语义求解器 + 种子注入 | ✅ 完成 | `phase4.2-llm-loop-complete` |
| **Phase 4.3** | 状态可行性（多步序列） | ✅ 完成（v0.4 起 E 臂;2026-10-14/15 目标块直导最小闭环,见下） | |
| **阶段1 P0-A/P0-B** | 输入级状态隔离(ExecutionContext/生命周期后端)+ 运行时状态观测(三值证据:verified/proxy/unknown 分桶) | ✅ 完成(协议默认 noop/无观测,`--lifecycle/--observe-state` 显式开启;A–F 行为不变) | |
| **Phase 0(新计划)** | 架构审计与统一状态契约:`docs/architecture/` 四件套(审计报告/状态契约/整合计划/决策记录) | ✅ 契约 v1.1b 定稿(关系约束 §6.6、证据/可观测性两维、指标四分离) | |
| **关系语义工作线(No-LLM)** | 关系约束 IR+静态提取+联合求值+WAL 安全快照;PR-00~03/M0 见下节 | 🔄 进行中:关口 A(M0-A)、PR-00/01/02/03 完成;M0-B(逐请求联合验证)/M0-C(自动规划)/PR-04~07 待做 | |

### 目标定向构造(goal)最小闭环(2026-10-14/15,未冻结)

P4.3 的延伸:从"未覆盖目标块"全自动构造状态。交付链(文档
docs/phase1-mvp.md … docs/real-app-first-blood.md):

1. **统一状态读写索引**:容器 dict 库词汇(`_DB[t][k]`/行变量别名/
   ORM 事务点)+ 读点→写点查询(`writers_of`);t_state_shop 四个
   历史盲区(5A 不可见)全入索引;
2. **目标块→约束(goal_derivation)**:必要分支边(支配近似,带
   true/false 方向;false 边 EQ→NE 反转)→ CONTROL 读点谓词集;
   ship_privileged → [EXISTS orders, EXISTS uid, EQ owner=uid];
   越权分支 → NE owner≠uid;
3. **自动链(goal_to_plan)**:op 分派臂发现 + 事实合成 AppModel +
   plan_backward + 谓词驱动参数(零预设名;可选覆盖参数不填)+
   身份刷新步(NE 约束 → distinct 值生产者重放);
4. **执行验证(plan_executor)**:C10 单步执行 + TraceSession 写块
   覆盖判据 + 整链重规划;t_state_shop/chain/gate 全判达;
5. **检查点(goal_checkpoint)**:判达链存档(未判达不存)→ C10 种子
   注入语料(goal_spool 惯例)→ 复用重放零推导再判达;
6. **三臂评测(goal_directed_eval)**:goal 5/5(cost 2-4 请求)vs
   随机单发 0/5 vs 随机序列 2/5(cost 高一个量级);冷启动 reset
   语义是关键方法论(状态泄漏会使对照失效);
7. **真实应用端到端(goal_app_bridge + http_plan_executor)**:
   linkding(生产级 Django)归属深块自动判达——两段式(静态在主
   环境/执行在应用 venv),Django 框架补全(@login_required→需
   auth,内置 LoginView→产 auth)与 LLM relate 互补;
8. **F 臂(goal_seed_injector)**:目标块直导注入器进 four_arm/
   campaign(契约同 E 臂注入器);spot E vs F:chain +3(F 更稳),
   gate 持平。20 真实应用靶标(apps20)接入:docs/targets-real-apps.md
   (gates 结果表 results/targets20/gates.md,不入库)。

### 关系语义工作线(No-LLM,2026-10-18/19,进行中)

上一节的 goal 闭环依赖"目标块→单主体谓词"推导;本工作线解决其
最深缺口——**状态间语义关系**(如 `order["user_id"] != user["id"]`
的归属约束)的自动发现与联合验证,不写业务规则、不用 LLM。契约与
计划见 `docs/architecture/`(STATE_CONTRACT v1.1b / INTEGRATION_PLAN,
实施依赖图 PR-00→…→PR-07,M0 拆 A/B/C):

1. **关口 A / M0-A(关系 IR+静态提取)**:`state/relational.py`
   (Field/Const/RelPredicate + 路径条件 + 来源链 + 单点结构求值)、
   `state/provenance.py`(跨函数 SELECT 表绑定/认证主体/路由参数/
   dict-store 别名)、`derive_relational_goal`(词法早退方向分析 +
   wrapper 条件化)——vulnshop `ship_order` 的四条件(EXISTS/归属
   EQ/状态 EQ/认证)零手写规则自动提取,改名实体(ticket/acct)
   同机制泛化;25 项专项测试;
2. **PR-01(CFG 正确性)**:修复 `necessary_guard_edges` 在含调用
   函数上的静默退化(函数内投影 CFG:可正常返回调用点补隐式
   callsite→returnsite 边、全路径 raise 不连、目标不可达诚实返回空)
   ——ship_order 必要守卫从"5 个全误报"收敛为 2 个真支配边;
   `guard_analysis_diagnostic` 提供支配/关系两层一致性诊断;
3. **PR-02(WAL 安全快照)**:`runtime/snapshot/l2_sqlite.py` 双捕获
   模式——backup(输入级生命周期:SQLite backup API 页级快照,WAL
   已提交未 checkpoint 帧自动入模板,不删活跃 sidecar,恢复前
   BEGIN IMMEDIATE 持锁探针 fail-closed)/copy(fork-COW 位级冻结
   语义,遇 WAL 显式拒绝);10 项 WAL 测试;
4. **PR-03(结构化事实+联合求值)**:`StateObservation` v2 四可选列
   (subject/observed_value/value_kind/observed_at;NULL/MISSING/UNKNOWN
   三值互斥)、vulnshop 观测器补 `User.id`/`Order.user_id`+属性级
   可观测性声明、`state/goal_evaluation.py`(ObservationView 单点
   视图 + evaluate_goal 候选有限枚举合取 + GoalAchievementWitness
   ——成功记账只来自单点完整 TRUE,不从历史原子拼合;认证主体关联
   显式化:DB 有 user 行 ≠ 请求已认证);13 项联合求值测试(任务书
   八反例全覆盖);
5. **当前状态**:全量测试 601 passed(基线随阶段增长,冻结口径见
   「冻结版本」节;关系线未冻结);样例产物 `artifacts/m0/`
   (relational_goal.json / goal_evaluation_sample.json);下一步
   M0-B(PR-04:五步已知链逐请求联合验证)。

---

## 模块详解

七域架构(P1 目录收敛后):**analysis / runtime / fuzzing / solving / state / adapters / evaluation**,外加原位不动的 core、cli、targets、fixtures、tests。

```text
stateforge/
├── core/           数据契约
├── analysis/       静态分析域(原 analyzer/ + static/ + graph/ 合并)
│   └── static/     CFG/ICFG 子包(cfg.py 名冲突按规则 8 以子包消解)
├── runtime/        运行时域(原 dynamic/ + executor/ + snapshot/ 合并)
│   └── snapshot/   ANOTA 快照子包(l2_sqlite 双捕获模式:backup/copy)
├── fuzzing/        Fuzzing 集成域(原 fuzz/,adapters/ 提取后)
│   └── feedback/   结构反馈子包(原 feedback/)
├── solving/        求解域(原 feasibility/)
│   └── llm/        LLM 基础设施子包(原 llm/)
├── state/          状态域(原位,吸收 planner/ → state/planning/)
│   ├── annotation/ 语义标注子包
│   ├── relational.py    关系约束 IR+结构求值(关系语义工作线)
│   ├── provenance.py    实体变量来源索引(跨函数 SQL/认证/路由)
│   └── goal_evaluation.py  单点观测视图+联合求值+witness
├── adapters/       目标适配层(原 fuzz/adapters/)
├── evaluation/     评测域(原 eval/ + experiment/ + viz/ 合并)
│   ├── experiment/ 实验基础设施子包
│   └── viz/        可视化子包
├── cli.py          命令行入口
├── targets/        测试靶场(原位不动)
├── fixtures/       真值基准数据(原位不动)
└── tests/          测试套件(原位不动)
```

| 域 | 一句职责 |
|----|---------|
| `stateforge/core/` | 数据契约:ProgramIR(结构 IR)+ AppModel(语义 IR)+ JSON 序列化 |
| `stateforge/analysis/` | 静态分析域:单函数 CFG 与全程序 ICFG(`analysis/static/`)、语义分析入口与模式匹配(`analyzer.py`/`patterns.py`/`selector.py`)、SSTG 图构建导出(`builder.py`/`export.py`)、XFG 执行流图(`cfg.py`) |
| `stateforge/runtime/` | 运行时域:`sys.settrace` 逐请求轨迹采集(`tracer.py`,untraced 观测窗口)、轨迹→ICFG 精确映射(`mapper.py`)、覆盖度量(`coverage.py`)、轨迹 I/O(`io.py`)、回放(`replay.py`/`snapshot_replay.py`)、快照子包(`runtime/snapshot/`:`l2_sqlite.py` backup/copy 双捕获——输入级 WAL 安全快照 / fork-COW 位级冻结,engine+supervisor 跨进程 fork-COW)、输入级执行上下文(`execution_context.py`:生命周期后端+观测编排+fail-closed 审计)、计划执行器(`plan_executor.py` 微靶 C10+写块判据 / `http_plan_executor.py` 真实应用 HTTP+块级判达,两段式) |
| `stateforge/fuzzing/` | Fuzzing 集成域:Atheris 子进程入口(`entry_driver.py`)、选择性字节码插桩、语料管理/候选/前沿采集、约束变异器;含结构反馈子包 `fuzzing/feedback/`(P2 结构新颖度 + P3 前沿定向距离/组合/预算) |
| `stateforge/solving/` | 求解域(P4 分支可行性):Guard 恢复→操作数观测→门内距离→输入溯源→约束求解→变异提示→停滞触发→LLM 种子注入;含 LLM 基础设施子包 `solving/llm/`(双协议 Provider/语义提升/缓存);goal 侧:`goal_checkpoint.py`(判达链检查点/C10 种子注入/复用重放)、`goal_seed_injector.py`(F 臂注入器,契约同 state_seed_injector) |
| `stateforge/state/` | 状态域:状态提取与语义模型(`extractor.py`:Session/ORM/容器 dict 库/两步守卫别名+关系 term 解析)、会话访问/状态溯源、运行时观测契约(`runtime_observer.py`:StateObservation v2 结构化列+属性级可观测性声明)、标注子包 `state/annotation/`、规划子包 `state/planning/`(原 planner/;`backward.py` 择优生产者+`compat.py` 应用兼容层);goal 侧:`goal_derivation.py`(必要分支边+方向感知约束+函数内投影 CFG 可达性+`derive_relational_goal` 关系目标)、`goal_to_plan.py`(op 臂→合成 AppModel→链→谓词参数)、`goal_app_bridge.py`(真实应用:Django 框架补全+AppModel 目标映射);关系语义工作线:`relational.py`(Field/Const/RelPredicate IR+单点求值+MISSING 语义)、`provenance.py`(变量来源索引)、`goal_evaluation.py`(ObservationView+evaluate_goal 联合求数+witness) |
| `stateforge/adapters/` | 目标适配层:TargetAdapter 协议 + 通用 Web/Flask/可调用/vulnshop 适配器(bytes → 目标可执行)+ `wsgi.py`(任意 WSGI callable→GenericWebAdapter,werkzeug 直驱) |
| `stateforge/evaluation/` | 评测域:SF-Micro 真值基准、BranchSolve 报告、OWASP Benchmark 基线、多臂实验子包 `evaluation/experiment/`(four_arm 含 F 臂;campaign GROUPS 含 F)、goal 三臂对照(`goal_directed_eval.py`)、可视化子包 `evaluation/viz/` |
| `stateforge/cli.py` | 命令行入口:analyze / feedback / experiment / viz / state |
| `stateforge/targets/` | 测试靶场:vulnshop / vampi / dsvw / pygoat / realworld / microplus + **apps20/**(20 真实应用靶标:harness+fuzz_target+NOTES 四件套,口径见 docs/targets-real-apps.md) |
| `stateforge/fixtures/` | 真值基准数据:micro / bs_micro / ls_micro / llm_loop / sf_branch_micro / buy_app(原位不动) |
| `stateforge/tests/` | 测试套件(全量绿,计数见冻结版本节;子目录名沿用旧模块名为历史组织,import 已全部指向新路径) |

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

### 目标定向构造(goal,2026-10-14/15,spot 口径)

| 实验 | 结果 |
|------|------|
| 三臂对照(stateful 三靶,60 请求预算) | goal **5/5 判达**(cost 2-4/目标)vs 随机单发 0/5 vs 随机序列 2/5(cost 12-27) |
| E vs F spot(2 runs×3 epochs×5s) | chain:E=36(一 run 33)/F=**39(两 run 全 39)**;gate 持平 39 |
| 真实应用端到端 | linkding(生产级 Django)归属深块自动判达(登录 302→edit 200,目标块覆盖);LLM relate 交叉验证一致 |
| 真 LLM 标注(glm-5.3-flash,Anthropic 代理) | healthchecks:103 端点需 auth+5 产 auth(全对);linkding 9 需+0 产(内置 LoginView 不可见→框架补全互补);缓存 4 文件入库 |
| D 臂 LLM 注入现状 | stateful 套件无 `.llm.py` 适配(硬检查静默跳过)——真 LLM 的 D 臂从未运行,待补适配(microplus 现存 2 个为 mock) |

---

## 冻结版本

| 标签 | 内容 |
|------|------|
| `phase1-freeze` | 静态/动态底座（21 真值满分） |
| `phase3-structural-freeze` | 前沿定向（10 runs 停止规则） |
| `phase4.2-llm-loop-complete` | LLM 覆盖循环（12/12 Mock E2E + 6 targets） |
| `v0.1-baseline` | P0 基线冻结（2026-10-08）：Phase-1~4.3 M1 全量能力；依赖收口 pyproject（extras dev/test/fuzz）；fresh-clone → venv → install → pytest → demo 验收通过 |
| `v0.2-normalized` | P1 工程规范化（2026-10-08）：目录收敛七域架构；本地 CI（Makefile+ci.sh）；config 单一入口（stateforge/config.py）；logging 约定（core/log.py+cli -v） |
| `v0.3-architecture` | P2 架构收敛（2026-10-08）：三搬移解环；ARCHITECTURE.md 分层规范+import 边界测试（例外登记制）；PROTOCOLS.md 九契约+schema 稳定性测试；清扫（config 统一 legacy 族等 7 项） |
| `v0.4-stateful-core` | P3 stateful fuzzing 核心（2026-10-08）：M2 迁移发现；序列种子契约 C10；规划器→序列种子注入（policy=state）；实验臂 E+状态三指标 |
| `v0.5-evaluation` | P4 评测规范化（2026-10-08）：B/C 结转协议修复；campaign 五臂+消融；vulnshop AppModel 真路径；10-run 正式战役与论文口径表 |
| `v1.0-release` | P5 release/paper（2026-10-09）：ARTIFACT.md 评估者入口+manifest；论文 evaluation 数字注入（EN/ZH 双版同步）；挂账终清扫 6 项；顾问规范化 P0-P5 全程完成 |

**v0.1-baseline 验证环境**:Python 3.13.5(anaconda)/ Ubuntu(Linux 6.8)/ clang 备用(atheris 实走 tuna wheel)/ pytest 8.3.4 · flask 3.1.0 · networkx 3.4.2 · elementpath 5.1.4 · httpx 0.28.1 · pydantic 2.11.7 · PyYAML 6.0.2(运行时)· matplotlib 3.10.0(dev)· atheris 3.1.0
**验收数字**:开发树 255 passed / 1 skipped / 26 deselected;fresh clone 254 passed / 2 skipped / 26 deselected(差异=OWASP Benchmark 用例按设计跳过,external/ 不入 git);micro_benchmark 21 用例全 1.00(CFG/CALL/RET P&R + trace-accuracy 5/5)

**v0.2-normalized 验收**:./ci.sh 全链绿(fresh clone 254 passed / 2 skipped / 26 deselected;micro_benchmark 静态 21/21 + 动态 5/5 全 1.00);开发树 255/1/26;零测试断言改动(tests 仅 import 路径机械同步)、零算法改动(ANOTA 侧零引用)

**v0.3-architecture 验收**:./ci.sh 全链绿(fresh clone 285 passed, 2 skipped, 26 deselected;micro_benchmark 静态 21/21 + 动态 5/5 全 1.00);开发树 286 passed / 1 skipped / 26 deselected;新增测试 31 项(边界 4+协议 17+config 10);零既有测试断言改动、零算法改动

**v0.4-stateful-core 验收**:./ci.sh 全链绿(fresh clone 349 passed, 2 skipped, 26 deselected;micro 静态 21/21+动态 5/5);开发树 350 passed/1 skipped/26 deselected;五臂 spot(口径:2 runs × 3 epochs × 5s 小样本):E−D=+16/+19 块,状态原子判达 E 4/4 run vs D 0/4 run(同机制),B–D 块/边同种子逐数一致;A 吞吐差 ≤7.7%(短预算噪声带);口径注:非 session 靶(microplus)E 落后 D,状态主张限定 stateful 靶族;t_state_shop 零注入=M1 session 词汇边界(5A DB 域守卫不可见)。臂 E 现仅经 `four_arm --suite stateful` 提供;campaign 战役组套(`runner.GROUPS`)未含 state 臂(P4 范围)(v0.5 已接入)。注(v0.5 后发现):当时 D 臂 llm 注入因判停缺陷未实际触发,D 实效=定向反馈;E−D 解读应为 E vs 定向反馈

**v0.5-evaluation 验收**:10-run 配对战役(3×10s/seed0=7):E−D1 块增益 t_state_chain +20.2、t_state_gate +22.0、vulnshop +99.9(median 98,10/0/0 全胜,AUC +0.0967);消融:规划器贡献 E−E-rand +2.4/+5.5/+65.6(真实应用上最大),R2 触发代价 |E−E-always| ≤1.1(≈零);vulnshop E 状态原子 union=4(注:该原子口径为 campaign 侧 AppModel 分派;消融侧三指标仅微靶路径可测,vulnshop 为未测)。**口径注(v0.5 起)**:B/C/D1/D2R/D2P 轮间语料结转自 v0.5 恢复(a0cc5bf..v0.4 期间失效),该窗口内上述臂的战役数字与 v0.5 及更早阶段不可直接比较;E 同窗口亦自 epoch 2 空 corp_in 起跑(结转门当时同样未覆盖 policy=state),该效应对 E 方向保守(缺结转只会低估 E 的覆盖);Phase-3 表中 D 族数字早于 a0cc5bf,口径一致可比;战役/消融数据在 results/experiment/p4_formal/(不入库),表值=p4_analysis 输出抄录

**v1.0-release 验收**:./ci.sh 全链绿(fresh clone 440 passed / 2 skipped / 26 deselected;micro 全 1.00:静态 21/21 + 动态 5/5);开发树 441 passed / 1 skipped / 26 deselected;论文两版注入数字源=p4_report(114 数字 token 双版程序化同步;本机无 LaTeX 工具链,两版未编译——见文件头注);已知留档不修项 10 条见 docs/known-debt.md(主要为观感/文档级);**披露:papers/draft/{main,main-zh}.tex 经 git add -f 入库(原在 .gitignore),如需移出 git rm --cached 后自行保管**

**goal 期验收(2026-10-14/15,未打 tag)**:开发树 **496 passed / 1 skipped /
26 deselected**(v1.0 的 441 → +55:顾问诊断修复 9 + goal 全链 46);
顾问 5 诊断修复(b543713);20 真实应用靶标 apps20 四件套(0c4dc1a,
FlaskBB 出局→MoinMoin 顶替);goal 最小闭环 8 件(65cb186…a48b649,
docs/phase1-mvp.md … docs/real-app-first-blood.md);F 臂(ca4b6ad);
真 LLM 缓存 4 文件入库(e844b5d,README 已知义务照办);核心契约词表
扩充 2 处(HttpMethod +HEAD/OPTIONS、AppModel.framework +synthetic,
协议测试零回归)。

**阶段1 P0-A/P0-B(63f8a23,未打 tag)**:输入级状态隔离
(ExecutionContext + Noop/Reset/Snapshot 生命周期,fail-closed 审计)+
运行时状态观测契约(StateEvidenceLedger 三值证据,C8 `state_evidence`
演进键);A–F 默认行为逐字节不变(开关 `--lifecycle/--observe-state`)。

**关系语义工作线(2026-10-18/19,进行中,未冻结)**:开发树 **601 passed /
1 skipped / 26 deselected**(goal 期 496 → 阶段1 +45 → 关系线 +60:
关口 A 25 + PR-01 8 + PR-02 10 + PR-03 17)。交付:契约四件套
(docs/architecture/,v1.1b)、关系 IR+提取(关口 A)、函数内投影 CFG
修复(PR-01)、L2 backup/copy 双模式(PR-02)、StateObservation v2+
goal_evaluation 联合求值(PR-03);样例产物 artifacts/m0/。下一里程碑
M0-B(PR-04:五步已知链逐请求联合验证)——进度与依赖图以
docs/architecture/INTEGRATION_PLAN.md 为准。

**已知义务:** 语义标注缓存(`stateforge/solving/llm/cache/*.json`)键于 PROMPT_VERSION 与夹具内容——二者变更后需重跑生成器并提交新缓存,否则 fresh clone 回归"全 irrelevant"。缓存仅按显式路径提交(`git add stateforge/solving/llm/cache/<hash>.json`),勿在该目录使用 `git add -A`。

---

## 技术栈

- **Fuzzing 引擎**：Atheris 3.1.0（libFuzzer for Python）
- **Oracle**：ANOTA 解释器（CPython 3.10.13 fork，NDSS'25）
- **静态分析**：Python AST + PyCG（ICSE'21）
- **LLM**：OpenAI 兼容 / Anthropic 双协议（GLM/Claude/local 可换）
- **测试框架**：pytest（全绿）

## 依赖

依赖唯一来源是 `pyproject.toml`（extras：`dev` / `test` / `fuzz`，版本=实装验证值）。安装见[快速开始](#快速开始)。ANOTA 解释器侧（anota/ CPython fork）锁定栈另见 `stateforge/requirements-anota.txt`，不属于本项目 extras。
## License

Proprietary — Research use only.
