# SF1 —— 理解层:Security-Relevant State 建模(State-Augmented Program Representation v1)

> 状态:待启动 | 工期:5-7 天 | 依赖:SF0 | 上游:[00-overview.md](00-overview.md)
> 概念定位:本里程碑的产物是 **State-Augmented Program Representation(SA-PR)v1**——用户架构中 SA-CPG 的瘦身版:保留状态增强的全部语义(State/Transition/Constraint/SecurityTarget),推迟 CPG 骨干(跨文件调用图/CFG/Neo4j,v2)。**每条状态事实带代码锚点(file:line)**,保住"程序表示"性质,v2 升级无需重新提取。

## 1. 目标

把 Flask/connexion 应用的源码变成结构化的 `AppModel`(端点 + 参数 + 前置条件 + 效应 + 守卫 + sink + 观测字段),并构建 NetworkX 状态图(二部 AND/OR,ADR-0006;论文术语 **SSTG**,Security State Transition Graph)。**职责边界(ADR-0009):analyzer 不裁决状态,只产候选**——流水线:`AST → 端点 IR → StateCandidate 挖掘 → (规则|LLM)语义标注 → StateVar → 确定性选择器 → SSTG`;本里程碑 LLM 侧只做**接口桩 + 规则标注(启发式列)**,LLM 实装在 SF6。**核心主张:不建模全部状态,只建模 Security-Relevant State**(认证/授权/所有权/资金/工作流进度/单次性**六类**)。

## 2. 任务分解

### T1 core/model.py(0.5d)
pydantic v2 数据模型(驱动侧;监督侧只用 stdlib):
`HttpMethod / Param(name, location∈{path,query,json,header,cookie}, py_type, value_domain)` `StateCandidate(var, entity, attr, ops∈{read,compare,write}, evidence, source∈{session_access,db_field,condition,ownership_binding}, file, line)` `StateVar(候选标注后的晋升态;entity, attr, kind∈{db_column,session,derived}, domain, label∈{authn,authz,ownership,money,workflow,single_use,irrelevant}, security_relevant: bool, rationale: str)` `Effect(var, op∈{set,clear,check}, value, key, file, line)` `Precondition(var, op∈{==,!=,in,truthy,falsy}, value, key, file, line)` `Guard(kind∈{role,owner,token,custom}, present, detail, file, line)` `Sink(kind∈{sql_build,file_path,exec,redirect}, file, line, snippet)` `Endpoint(func, route, methods, params, guards, preconditions, effects, sinks, observes)`。
含 `AppModel(app_root, framework, endpoints, state_vars, invariants, config_flags)` + `to_json/from_json`。

**SecurityTarget 六类标签(漏洞锚定;分类贴近漏洞族而非泛化状态分类——与 Joern 系"有结构无语义"表示的差异点,论文素材)**:

| 标签 | 识别特征 | 锚定漏洞族 |
|---|---|---|
| `authn` | Session.authed_as / token 类 | 认证绕过 / 会话劫持 |
| `authz` | role / permission | 越权提升 |
| `ownership` | owner_id / user_id 绑定 | IDOR |
| `money` | amount / total / balance / paid_* | 支付绕过 |
| `workflow` | 状态机枚举属性 | 订单 / 流程 bypass |
| `single-use` | used / consumed 类布尔 | 优惠券 / 单次性重用 |

打标结果供三方消费:创新 3 的 checkpoint 评分 α 项、创新 1 LLM 提升的修正目标、oracle 的不变式强制范围。

### T2 双路由适配器(1.5d)⚠️ VAmPI 的前提(ADR-0003)
- [ ] `flask_adapter.py`:`@app.route` / `@bp.route` / `@app.get|post|put|delete`(2.0+ 快捷方式)、blueprint `url_prefix` 拼接;`<int:id>`/`<username>` → `Param(location=path)`
- [ ] `openapi_adapter.py`(VAmPI 专用):解析 `openapi_specs/openapi3.yml`(PyYAML)取 `paths → (method, operationId, parameters, requestBody schema)`;`operationId`(如 `api_views.users.register_user`)反查 `api_views/*.py` 同名函数取 AST。**VAmPI 路由 100% 在 spec 里,没有此适配器提取不到任何端点**
- [ ] 适配器自测:对 vulnshop(vuln+anota 两变体)与 VAmPI 各产出路由表,golden 断言条数与路径

### T3 AST 模式规则(2d)`patterns.py`
| 模式 | AST 形态 | 产出 |
|---|---|---|
| 会话写 | `session["k"]=v` / `session.pop("k")` | `Effect(Session.k, set/clear)` |
| SQL 拼接 | `execute("SELECT.."+x)`、f-string、`%` 格式化(串首 SQL 关键词) | `Sink(sql_build)` |
| SQL 常量写 | `execute("UPDATE orders SET status=?..",..)` 常量可解 | `Effect(Order.status, set)` |
| ORM 写 | `db.session.add(Model(..))`+`commit`;`obj.attr=<literal>`(attr∈状态属性集) | `Effect` |
| ORM 查询守卫 | `Model.query.filter_by(id==..)` | 实体键绑定 |
| 枚举比较 | `obj.status=="PAID"`、`if not coupon.used` | `Precondition` 候选 + `StateVar.domain` 挖掘 |
| 角色检查 | `if user/admin`、`token_validator(...)['admin']`、`@requires_admin` | `Guard(role, present)` |
| 认证链 | `jwt.encode/decode`、`Authorization` 头 | `Session.authed_as` 产/需 |
| 请求输入 | `request.get_json()/args/form/headers.get()` | `Param` schema |
| 响应构造 | `jsonify({...})` / `Response(json.dumps({...}))` 字典键 | `observes` |
| 全局旗标 | 模块级 `vuln=int(os.getenv(...))` / `GUARDS` | `config_flags`;`if vuln:`/`if GUARDS:` 裁真分支(vuln=1 默认,GUARDS 双态都提取) |

**产出统一为 StateCandidate 记录**(var+ops+evidence+file:line),跨端点汇聚(同 var 合并、domain=字面量集)后交标注层;**patterns 不做安全裁决**(ADR-0009)。

### T4 惯用法字典(1d)`idioms.py`
纯数据表(yaml):flask / connexion / sqlalchemy / sqlite3 / PyJWT / hmac(vulnshop token)五组,每条 `(模块 or 属性链, 函数名, 参数角色, 产出类型)`。新增框架只加数据不动 matcher。

### T5 helper 单层内联(1d)
对目标内定义、参数 ≤3、无递归的函数(如 VAmPI `token_validator()`)做**单层调用点内联**(callee 的 returns/分支结论折算到调用点)——分析 VAmPI 认证逻辑的最小必需深度。不做跨文件过程间数据流。

### T6 状态图构建(1d)`graph/builder.py`
`nx.MultiDiGraph` 二部图(论文术语 SSTG):`state` 节点(`"Order.status=PAID"`,属性含 StateVar/value/**security_relevant**)↔ `action` 节点(`"POST /orders/<id>/pay"`,属性含 Endpoint + 代码锚点);`requires` 边 action→state(多条=AND);`produces` 边 action→state(`{op, key}`);**同实体状态原子之间加 invariant 边**(`legal: true/false`,来自提升层 legal_transitions——invariant oracle 与 forbidden_transition goal 直接查询);认证链=普通路径(`pay ← authed_as ← login ← registered ← register`)。`export.py` → `stategraph.json`(nx.node_link_data)+ dot 可视化(security_relevant 节点高亮)。

### T7 标注桩 + 启发式(1d)
- `llm/lifting.py`:接口两段(ADR-0009)——`label(candidates) -> [StateVar]`(逐候选打六类标签+rationale)+ `relate(relevant_vars) -> LiftResult`(只对 security-relevant 候选做关系提升:角色层级/间接守卫迁移/跨实体不变式;上下文小、可缓存、可审计);`provider=None` 时直接走规则标注
- **legal_transitions 主通道 = 静态 guard→effect 配对**(同端点 precondition×effect → 迁移边;代码实现的迁移即 ground truth);启发式/LLM 只补 LOW_CONFIDENCE 缺口,不整体生成状态机
- `analyzer/selector.py`:确定性选择器,**固定类→策略映射**(authn/authz/ownership/money/single_use 直接入图;workflow 需 ≥2 字面量比较即有真实状态域;irrelevant 出局,静态特征强时标记待审)——无加权分、无阈值,规则随论文发表
- `llm/heuristic.py`:规则标注(session/token→authn;role→authz;owner_id→ownership;amount/balance→money;枚举属性→workflow;used/consumed→single_use)+ 枚举挖掘(domain=字面量集)+ 线性序兜底(CREATED<PAID<SHIPPED 按端点动词序)
- 验收点:启发式列(规则标注+静态配对+选择器)在 vulnshop 上产出的状态机必须与人工期望一致(vulnshop 枚举显式)

## 3. 已知局限(写进产出报告,不静默)
无跨文件数据流(除单层内联);Jinja 模板内逻辑不分析;动态分发(getattr/dict→func)与 class-based view 跳过;路径转换器缺失由 planner `@probe` 兜底;`if vuln:` 裁剪依赖旗标已知。

## 4. 测试

- **golden**:`stateforge/targets/vulnshop/golden/AppModel.golden.json`(手写期望)与 analyzer 产出 diff;VAmPI 同样但按"端点覆盖 100%、效应人工核对 ≥70%"标准
- 单元:每个 pattern 规则一个最小函数用例(合成代码片段 → 期望产出)
- 回归:analyzer 对 `exp/dsvw_anota.py` 跑通不崩(DSVW 非 Flask,只验证不炸)

## 5. 验收清单

- [ ] vulnshop:端点 15/15(两变体),effects/preconditions 与 golden diff ≥90%(遗漏显式列出)
- [ ] VAmPI:openapi 路由覆盖 = spec paths 100%;SQLAlchemy effects 人工核对 ≥70%;`if vuln:` 双分支裁剪正确
- [ ] 启发式提升: vulnshop 状态机与人工期望一致(CREATED→{PAID,CANCELLED}, PAID→SHIPPED, used 单向)
- [ ] StateCandidate 覆盖:四类候选源(session_access/db_field/condition/ownership_binding)在 vulnshop golden 逐一核对无遗漏
- [ ] **SA-PR 三修正落地**:① 每条 Effect/Precondition/Guard 带 file:line 锚点(golden 校验);② security_relevant 打标覆盖六类标签且 vulnshop 上与人工标注一致;③ invariant 边入图(invariant oracle 可直接查询)
- [ ] `stategraph.json` 导出 + dot 渲染可看(security_relevant 高亮);局限清单成文(`stateforge/analyzer/LIMITATIONS.md`)

## 6. 风险与回退
| 风险 | 回退 |
|---|---|
| VAmPI ORM 模式识别率低 | 先收窄 sqlalchemy 惯用法到 `Model.query.filter_by`+`db.session.add` 两型;LOW_CONFIDENCE 标记交 LLM/人工(SF6 量化增量) |
| openapi schema 复杂(allOf/$ref) | v1 只展平一层 $ref;复杂 schema 降级为宽松 Param(str) |
| 内联引入误报 | 内联产出带 `inlined_from` 溯源;golden diff 单列内联贡献 |
