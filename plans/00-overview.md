# StateForge 总览(v1)

> 版本:v1.1(2026-09-17,修订 R1:ADR-0009 候选锚定标注)| 状态:开发中 | 上游依据:`~/.claude/plans/foamy-chasing-moth.md`(已批准)+ 设计评审(2026-09-17)+ 三轮设计修正讨论(2026-09-17)
> 定位:**State-aware Deep Exploration Framework for Python Web Applications**——面向深层业务状态的状态恢复/构造/探索系统

## 1. 一句话与核心思想

传统 Fuzzer 把 Web 应用当**函数**(输入→输出,等 crash);StateForge 把应用当**状态机**——**漏洞 = 可达的非法状态或非法转移**(IDOR / workflow bypass / coupon reuse / payment bypass / 越权)。

工作流:

```
源码 ──> 理解层(analyzer:AST→端点 IR→StateCandidate 候选挖掘)
      ──> 语义提升层(规则标注 | LLM Semantic Lifting:候选→六类安全标签+rationale)
      ──> 确定性选择器(固定类→策略) ──> SSTG 状态图(NetworkX 二部 AND/OR)
      ──> 反向规划器(目标状态→动作序列)
      ──> 执行层(Atheris fuzz 循环 + 确定性重放)
      ──> 快照层(fork COW 检查点树 + L2 文件 DB 模板恢复)
      ──> 判定层(不变式 / 授权重放 / ANOTA 注解桥 / 响应差分)
```

论文叙事:ANOTA+FUZZER = Atheris + 注解 oracle(**无状态层**);StateForge = Atheris + 注解 oracle + **状态构造/恢复**(贡献与 fuzzer 正交)。创新1 正式命名 **Security-Relevant State Recovery**(方法名 LLM-guided Semantic Lifting)——状态在源码里,我们从代码**恢复**它;动机锚点:Joern/CodeQL 系表示有 AST/CFG/PDG 但无程序状态语义(`account.status=2` 是整数赋值,不知 2=特权账户),语义缺口由提升层补;LLM 产出**可审计标签集**(枚举标签+rationale+file:line),不进检测环(检测链确定性,B0/B1/B2 消融成立)。

## 2. 立项依据(已验证事实,2026-09-17 实测)

| 事实 | 数据 | 出处 |
|---|---|---|
| ANOTA 判定信道 | `ANOTA_EXECUTION.BLOCK(cond, msg="ANOTA_POLICY:<名>")` → RuntimeError → 500 响应体可归因 | `exp/harness_dsvw.py:policies_in` |
| 注解 oracle 矩阵 | 20 用例 TP11/FN0/FP0/TN9(基线=快照=depth-1 恢复点,三者一致) | `exp/results/*.json` |
| fork 恢复成本 | depth0 0.49ms(~2000/s),depth1/2 0.65ms,**与链长无关** | `exp/results/atropos_snapshot_results.json` |
| epoch 摊销 | n=1→3.6ms/输入,n=10→0.93,n=50→0.58 | 同上 |
| 完整重启 | ~65ms → 快照恢复 ≈ 重启的 1/77 | 同上 |
| **二阶乘积律** | baseline=DETECT / per-input=MISS(0 FP) / epoch(n=4)=DETECT+隔离 → **检出 = 使用点 oracle × 状态链,缺一不可** | `exp/results/second_order_results.json` |
| 超时恢复 | itimer 0.4s 杀叶、检查点无损 | 同上 |
| VAmPI 真实结构 | **connexion 2.14.2 路由(在 openapi3.yml,不在代码)+ Flask 2.2.2 + flask-sqlalchemy + PyJWT;`vulnerable` 环境旗标;文件型 SQLite** | 设计评审核实 |

## 3. 进程拓扑(核心架构决策 ADR-0001)

```
┌─────────────── 驱动进程(system python3.13,已有 networkx/httpx/pydantic)─────┐
│ analyzer / graph / planner / oracle / eval / llm(不依赖 ANOTA)               │
└──────▲───────────────────────────────────────────────┬──────────────────────┘
       │ AF_UNIX + JSON + SCM_RIGHTS(与 exp/atropos_snapshot.py 协议字节兼容)  │
       │                                                  │ 确定性重放(HTTP/WSGI 桥)
┌──────┴──────────────────────┐                 ┌────────▼───────────────────┐
│ 快照监督进程(ANOTA 解释器)      │  fork COW 叶    │ Atheris fuzz 进程(ANOTA)     │
│ stateforge/snapshot/         │ ──────────────▶ │ TestOneInput: 字节→动作→执行   │
│ supervisor(冻结为 depth-0)    │  每叶 = 1 epoch  │ 双模式:in-process / snapshot  │
└──────────────────────────────┘                 └────────────────────────────┘
```

- **监督进程与叶必须在 ANOTA 解释器下**(fork 继承 ANOTA 运行时,注解 oracle 才在叶内生效);
- **分析/规划/评测驱动用系统 Python**(pydantic/networkx/httpx 生态,不污染 ANOTA fork);
- 两进程族经 AF_UNIX 文件 socket 通信(`socket.send_fds/recv_fds` 对任何 AF_UNIX SOCK_STREAM 成立,`exp/` 的 `_send/_recv` 原语零改动复用)。

## 4. 模块地图

```
stateforge/
├── core/       model.py(pydantic 全模型) errors.py
├── analyzer/   analyzer.py flask_adapter.py openapi_adapter.py patterns.py idioms.py summarize.py selector.py
├── graph/      builder.py stategraph.py export.py
├── planner/    backward.py binding.py goals.py
├── fuzz/       atheris_harness.py action_codec.py          # Atheris 执行器(ADR-0002)
├── executor/   replay.py extract.py session.py            # 规划链确定性重放
├── snapshot/   engine.py supervisor.py client.py l2_sqlite.py wsgi_bridge.py protocol.py policy.py
├── oracle/     registry.py invariant.py authz.py anota_bridge.py response_diff.py report.py
├── llm/        provider.py lifting.py heuristic.py cache.py
├── targets/    vulnshop/(app.py app_anota.py adapter.py golden/ tests/) vampi/(原码+adapter+probes/)
├── eval/       runner.py baselines.py metrics.py tables.py
└── cli.py
```

`exp/` **冻结为论文 artifact**,不改;复用一律"拷贝+改造+文件头注明出处"。

## 5. 里程碑索引(细节见各 SF 文档)

| 里程碑 | 文档 | 工期 | 出口标准(一句话) |
|---|---|---|---|
| SF0 底座 | [SF0-foundation.md](SF0-foundation.md) | 4-5d | 双靶场在 ANOTA 下可服务;**Atheris-on-ANOTA 硬门禁冒烟通过** |
| SF1 理解层 | [SF1-analyzer.md](SF1-analyzer.md) | 5-7d | vulnshop golden 100% / VAmPI 端点覆盖 100%、效应 ≥70% |
| SF2 Fuzz 执行+快照 | [SF2-fuzz-executor.md](SF2-fuzz-executor.md) | 6-8d | 双模式 harness 可跑;L2 零漂移;覆盖反馈 spike 出结论 |
| SF3 规划层 | [SF3-planner.md](SF3-planner.md) | 5-7d | 5 类 goal 各 20/20 达成;bypass 链实际到达非法状态 |
| SF4 判定+评测 | [SF4-oracle-eval.md](SF4-oracle-eval.md) | 4-5d | vulnshop 5/5 检出、fixed 0 FP;B0/B1/B2 三列对比表 |
| SF5 快照调度(创新3) | [SF5-snapshot-policy.md](SF5-snapshot-policy.md) | 5-6d | 四策略消融:检查点↓≥50% 覆盖不降,重放↓≥70% |
| SF6 语义提升(创新1:State Recovery) | [SF6-llm-lifting.md](SF6-llm-lifting.md) | 5.5-7.5d | 逐候选标注 P/R + 规则 vs LLM vs 人工 gold 三方对标表 |

合计 35-46 人日 ≈ **8-10 周**(与"2-3 个月原型"一致)。

## 6. 关键决策记录(ADR)

| # | 决策 | 理由 |
|---|---|---|
| 0001 | 双进程拓扑(ANOTA 侧:监督/叶/Atheris;系统侧:分析/规划/评测) | ANOTA 是 fork 化 CPython,工具链生态不装进去;协议字节兼容 exp/ |
| 0002 | **执行器 = Atheris**(谷歌开源,libFuzzer 系) | 与 ANOTA+FUZZER 同款;贡献与 fuzzer 正交;基线直接对齐论文方法论 |
| 0003 | VAmPI 分析走 **OpenAPI/connexion 适配器**(代码内无路由) | 设计评审发现:VAmPI 路由 100% 在 openapi3.yml;没有此适配器提取不到任何端点 |
| 0004 | L2 文件 DB 恢复 = **位级模板拷贝 + journal 清理** | 模板=冻结时刻文件的位拷贝,与检查点父进程页缓存精确一致,无脏页检测问题 |
| 0005 | vulnshop 双变体用 **GUARDS 环境旗标**(单源码) | 对照 VAmPI `vulnerable` 旗标惯例;避免双源码漂移 |
| 0006 | 状态图 = **二部 AND/OR 图**(状态原子 ↔ 动作,NetworkX MultiDiGraph) | 避免超边;反向规划=标准反向链接;Neo4j 留 v2 |
| 0007 | 评测三基线 **B0 裸 Atheris / B1 ANOTA+Atheris / B2 StateForge** | 与 ANOTA 论文"ANOTA+FUZZER vs unmodified Atheris"方法论对齐 |
| 0008 | LLM 双适配 **OpenAI 兼容(含 GLM)+ Anthropic**,env 配置,httpx 直调不引 SDK | 用户决策;无 key 启发式兜底,链路永不阻塞 |
| 0009 | **候选锚定标注**:analyzer 只产 StateCandidate;LLM 只做候选语义标注(六类枚举标签+rationale)+ 对 security-relevant 候选的有界关系提升;legal_transitions 主通道=静态 guard→effect 配对;确定性选择器(固定类→策略,无加权分无阈值);LLM 不进检测环 | 可解释/可复现/幻觉受控(枚举约束,LLM 无法发明状态或迁移);审稿人质疑"状态定义依赖 LLM"的防线;B0/B1/B2 消融成立 |

## 7. 术语表

| 术语 | 定义 |
|---|---|
| 检查点(checkpoint) | 冻结的干净进程状态(depth-0 根或嵌套 depth-k);fork 出叶服务请求,自身永不处理流量 |
| 叶(leaf) | 从检查点 fork 的一次性服务进程;一个叶的生命周期 = 一个 **epoch** |
| epoch | 两次恢复之间的窗口:epoch 内状态累积可见,epoch 间一切清零(n=1 即逐输入恢复) |
| 状态原子(state atom) | `Entity.attr=value` 谓词,如 `Order.status=PAID`;状态图节点 |
| StateCandidate(状态候选) | 静态挖掘的未裁决事实:var+ops{read,compare,write}+evidence+来源(session/db_field/condition/ownership)+file:line;patterns 产出,不做安全裁决 |
| 语义提升(Semantic Lifting) | StateCandidate→StateVar(六类标签+rationale)的标注阶段;规则(启发式列)与 LLM 双实现,即对标实验两列 |
| SSTG | Security State Transition Graph,论文术语;即本项目二部 AND/OR 状态图(state atom ↔ action,requires/produces/invariant 边) |
| 动作(action) | 端点+具体参数模板;状态图另一类节点;requires(AND)/produces 边 |
| GoalSpec | 漏洞目标规格:reach_state / forbidden_transition / authz_replay 三类 |
| PlanSegment | 一条规划链 = 一个 epoch = 一个叶(二阶乘积律的工程落地) |
| B0/B1/B2 | 裸 Atheris / ANOTA+Atheris(注解,无状态层)/ StateForge 全家 |
| L1/L2/L3 快照 | 进程级 fork COW / 文件 DB 模板恢复 / CRIU·容器(v2) |

## 8. 文档索引

[SF0-foundation.md](SF0-foundation.md) · [SF1-analyzer.md](SF1-analyzer.md) · [SF2-fuzz-executor.md](SF2-fuzz-executor.md) · [SF3-planner.md](SF3-planner.md) · [SF4-oracle-eval.md](SF4-oracle-eval.md) · [SF5-snapshot-policy.md](SF5-snapshot-policy.md) · [SF6-llm-lifting.md](SF6-llm-lifting.md) · [e2e-test-plan.md](e2e-test-plan.md) · [tech-plan-v1.md](tech-plan-v1.md)(上游方案存档)
