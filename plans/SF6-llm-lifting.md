# SF6 —— 创新 1:Security-Relevant State Recovery(LLM-guided Semantic Lifting,双供应商)+ 三方对标实验

> 状态:待启动 | 工期:5.5-7.5 天 | 依赖:SF4(SF1 的桩已在) | 上游:[00-overview.md](00-overview.md)(ADR-0009) · gold standard:`exp/dsvw_anota.py` + vulnshop 注解

## 1. 目标

用 LLM 对 analyzer 产出的 **StateCandidate** 做**语义标注(Semantic Lifting)**:候选发现与状态选择保持确定性(静态挖掘 + 静态 guard→effect 配对 + 固定类→策略选择器,ADR-0009),LLM 只解释候选语义(六类枚举标签 + rationale),并对 security-relevant 候选做**有界关系提升**。与规则标注、人工 gold 三方同台对标——量化 LLM 的增量价值与成本。

**论文叙事**:创新1 = **Security-Relevant State Recovery**(状态在源码里,我们从代码**恢复**它;方法名 LLM-guided Semantic Lifting)。动机锚点:Joern/CodeQL 系表示有 AST/CFG/PDG 但无程序状态语义(`account.status=2` 是整数赋值,不知 2=特权账户),语义缺口由提升层补。每条标注带 file:line 锚点 + rationale,可审计;LLM 产出**标签集**,不进检测环(检测链确定性,B0/B1/B2 消融成立)。

## 2. 任务分解

### T1 provider 双适配(1d)`llm/provider.py`(ADR-0008)
- `SF_LLM_PROVIDER∈{anthropic, openai-compatible}`;`ANTHROPIC_API_KEY` → `POST /v1/messages`(x-api-key);`OPENAI_API_KEY`+`SF_LLM_BASE_URL`(GLM 等)→ `/chat/completions`
- **httpx 直调,不引 SDK**(驱动侧已有 httpx 0.28.1);超时 30s、重试 1 次
- `SF_LLM_MAX_CALLS` 预算上限;`llm/cache/`:以 `sha256(源码片段)` 为键磁盘缓存(可复现 + 成本封顶)
- 无 key → `provider=None` → 启发式,链路永不阻塞

### T2 候选锚定标注协议(2.5d)`llm/lifting.py`(ADR-0009)
- **输入**:逐候选 `StateCandidate`(SF1 产出:var + ops + evidence + kind + 代码上下文片段)——**不是整段函数摘要;LLM 不做发现**
- **输出 A `label(candidates)`**:每候选一枚举标签(六类 + irrelevant)+ rationale + confidence(强 JSON schema,pydantic 校验,失败重试 1 次落规则标注);标签写进 SF1 `StateVar.label/rationale` 字段
- **输出 B `relate(relevant_vars)`(有界)**:仅对 label∈六类的候选做关系提升——角色层级 / 间接守卫迁移 / 跨实体不变式;上下文小、逐条可缓存可审计
- **legal_transitions 主通道 = 静态 guard→effect 配对**(SF1 已建);LLM 只补 LOW_CONFIDENCE 缺口——代码实现的迁移即 ground truth,LLM 不整体生成状态机
- 提示词版本化(`llm/prompts/v{n}.md`),入缓存键(sha256(候选事实))——换提示词自动失效缓存;温度=0,模型与版本号记录进实验报告(可复现)
- 增量策略:规则标注打底,LLM 只对 LOW_CONFIDENCE 候选调用(SF1 标记的缺口)

### T3 三方对标实验(2d)
同一评测矩阵(SF4 协议)上跑三列:
| 列 | 状态机来源 | 成本 |
|---|---|---|
| heuristic(规则标注) | SF1 规则标注 + 静态配对 + 确定性选择器(零 LLM 调用) | 0 |
| llm(语义提升) | **同一**静态候选/选择器/图构建器,仅标注源换 LLM(双供应商各一遍,记成本/延迟)——LLM 贡献被干净隔离 | token 数 |
| human-gold | 手写状态机(vulnshop 规格 + `exp/dsvw_anota.py` 注解经验) | 人工时 |

指标:**逐候选标注 precision/recall**(对 gold;实体/状态域/迁移矩阵逐项)、下游端到端检出率(提升错误传导)、token 成本与时延、双供应商输出一致性、LLM 标签 vs 规则标签冲突清单(冲突率本身即实验结果)。

### T4 注解起草扩展(1d,可选)
- LLM 同时起草 ANOTA 注解(把状态机语义反向写成 `ANOTA_EXECUTION.BLOCK` 守卫)vs `dsvw_anota.py`/`app_anota.py` 人工注解——创新 1 完整闭环(起草→人工审→gold)
- 输出格式与 ANOTA 注解规范一致(注解必须在 try 覆盖区内,已踩坑规范)

## 3. 测试

- provider 单测:mock httpx 响应(双协议各一),断言解析/重试/缓存
- schema 校验单测:非法输出 → 重试 → 启发式降级
- 对标实验脚本:`run_eval.py --lifting {heuristic,llm,anthropic,llm-openai,human}` 三方同矩阵

## 4. 验收清单

- [ ] 双供应商实跑通(env 配置,缓存生效:同输入二次调用零 token)
- [ ] 三方对标表:逐候选标注 P/R + 端到端检出 + 成本/延迟(heuristic 与 llm 列共用同一选择器与图构建器)
- [ ] vulnshop 上:启发式 = gold(枚举显式,预期持平);VAmPI 上:LLM 增量量化(这是主结果)
- [ ] 提升→规划→检出 的错误传导分析(至少 1 个 LLM 误判案例的全程追踪)
- [ ] (可选)注解起草闭环 demo

## 5. 风险与回退

| 风险 | 回退 |
|---|---|
| 输出 schema 违规率高 | 结构化输出约束 + 1 次重试 + 启发式降级(已设计);提示词版本化迭代 |
| 成本失控 | MAX_CALLS 上限 + 磁盘缓存 + 增量策略(只调 LOW_CONFIDENCE) |
| 双供应商结果分歧大 | 一致性本身是实验结果(报告);主表用默认供应商,副表交叉验证 |
| LLM 标签与规则标注冲突 | 默认 LLM 覆盖规则、逐条留 rationale 审计记录;冲突率入报告 |
