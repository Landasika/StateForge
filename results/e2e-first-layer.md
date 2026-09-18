# 第一层垂直切片 —— 端到端验收报告(2026-09-18 晨)

> 范围:SF0(底座)+ SF1(理解层/SA-PR v1)+ SF6 核心(候选锚定标注 + ANOTA 注解起草)。
> 执行分支:`sf-first-layer`(冻结基线之后 6 个提交)。

## 1. 一图流(实际跑通的链路)

```
vulnshop(app.py, 11 路由)   ─┐
                              ├─> AST patterns → StateCandidate(未裁决)
VAmPI(openapi3.yml, 14 路由)─┘        │
                                     ├─ 规则标注(heuristic 列)── 六类标签
                                     ├─ LLM 语义标注(Semantic Lifting)── 真·GLM 代理实跑
                                     └─> 确定性选择器(固定类→策略)
                                              │
                                     SSTG(NetworkX 二部图,requires/produces/invariant)
                                              │
                                     legal_transitions(静态 guard→effect 配对 + 线性序兜底)
                                              │
                                     ANOTA 注解起草(5 DraftAnnotation 规则 D1-D5)
                                              │
                                     对照手写 app_anota.py 六策略 → recall = 1.0
```

## 2. 测试矩阵(全绿)

| 套件 | 解释器 | 结果 |
|---|---|---|
| `pytest -q stateforge/tests/`(golden/图/标注/提供者 mock) | 系统 py3.13 | **25 passed, 1 skipped**(skip=ANOTA 信道在驱动侧,按设计) |
| `pytest -m anota`(vulnshop PoC+信道+压测 / VAmPI 双态冒烟) | ANOTA py3.10 | **18 passed** |
| e2e(规则列) | 系统 | 2 靶标全通 |
| e2e(LLM 列) | 系统→GLM 代理 | **真 LLM 标注实跑**(bigmodel.cn anthropic 兼容层,thinking 已关,缓存生效) |

## 3. 关键数字

- **vulnshop**:11/11 路由;6 候选全晋升;**LLM 标签 = 规则标签 = 人工 gold(6/6 一致)**——符合 SF6 预测(枚举显式的靶标上 LLM 无增量,主战场在 VAmPI)
- **VAmPI**:14/14 端点覆盖(=spec 100%);3 个 security-relevant 变量(authn×2 + authz)
- **legal_transitions**:`CREATED→{PAID,CANCELLED}, PAID→SHIPPED`(静态配对)+
  `Coupon.used` 单向不变式;**LLM relate 补回了静态因参数化 SQL 漏掉的 `CREATED→PAID`**(在
  bigmodel 代理上,角色层级 `{admin:[user]}` 也恢复了)——ADR-0009 分工(静态出结构、
  LLM 补语义缺口)按设计工作
- **ANOTA 注解起草**:9 条草稿(带 file:line 锚点 + BLOCK 代码 + rationale);
  与 `app_anota.py` 手写 6 策略按语义类对照 **recall=1.0,零多余类**
- LLM 成本:vulnshop 16.2s / vampi 23.5s(含 relate);缓存后二次零 token

## 4. 复现命令

```bash
# 全部测试(驱动侧)
python3 -m pytest -q stateforge/tests/
# ANOTA 侧(靶场+信道)
anota/ANOTA/ANOTA-interpreter/python -m pytest -q -m anota stateforge/tests/targets
# e2e(自动探测 provider:ANTHROPIC_AUTH_TOKEN+BASE_URL 即本机 GLM 代理)
python3 -m stateforge.cli e2e          # LLM 列
python3 -m stateforge.cli e2e --no-llm # 规则列
# 产物:results/{vulnshop,vampi}.stategraph.{json,dot} + e2e-first-layer.json
```

## 5. 与计划的偏差(全部记录在案)

1. **werkzeug 2.3.8 → 2.2.3**:connexion 2.14.2 硬性要求 <2.3(ResolutionImpossible;已回写 SF0 计划)
2. **T5 单层内联未做通用版**:VAmPI `token_validator` 经 idioms 调用名分派识别(LIMITATIONS.md 已列)
3. **coupon 建模修正**:置位恒发生、vuln=不查(否则状态不可观测,三类 oracle 全盲;已回写计划)
4. **idioms.yaml**:内置默认表,外部 yaml 加载接口就绪(未单列文件)
5. Joern spike / R2 文档修订(Gap/θ/B梯子/核心公式)/ SF2-SF5:按用户指示押后

## 6. 已知缺口(明天验收时可问)

- VAmPI 侧 SQLAlchemy 效应提取仍浅(books 的 secret 等;≥70% 人工核对的 milestone 出口标准尚未做人工核对——那是 SF1 验收项,非本切片)
- `request_data.admin` 等非归一候选保留(实体归一推给 relate/v2)
- 注解起草是**草稿工件**(人审后应用),未自动写回靶场——SF6-T4 的"起草→人审→gold"闭环设计如此
