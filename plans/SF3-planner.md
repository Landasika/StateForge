# SF3 —— 规划层:目标状态反向规划 + 运行期参数绑定

> 状态:**核心达成(2026-09-19,决策 B 先行)** | 工期:5-7 天 | 依赖:SF1(状态图),SF2-T3(执行器,已提前实现) | 上游:[00-overview.md](00-overview.md)
> 进度:T1 GoalSpec ✅ T2 反向 BFS+拓扑排序 ✅ T3 绑定(@fresh/?var/嵌入式槽/响应提取)✅ T5 直跑联调 ✅(ANOTA 子进程服务 + httpx 真 HTTP;10/10 格阵:vuln 全 hit / fixed 全阻断)
> 实装补充:soft-producer 兜底(参数化 SQL 盲区:pay 的 status=? 由"观测+item 级端点"启发 + 运行时 verify 裁决);drop_requires 攻击假设模式;资源/认证链完备化规则
> 未竟:T4 规划链→fuzz 词汇表(随 SF2);20/20 重复阵;VAmPI goal 套件;replan 环(观察偏离触发,含 Gap 形式化——R2 押后项)

## 1. 目标

实现"goal → action sequence"的反向规划器:给定 GoalSpec(目标状态/非法迁移/授权重放),在状态图上反向搜索动作链,运行期把抽象参数(?oid/?token)绑定到具体值,产出可执行的 PlanSegment(= 一个 epoch)。规划链同时作为 SF2 fuzz harness 的**动作词汇表/种子**。

## 2. 任务分解

### T1 GoalSpec 库(1d)`planner/goals.py`
每漏洞类一个目标模板(vulnshop 5 类 + VAmPI 3 类):

| GoalSpec | kind | 语义 |
|---|---|---|
| `goal_workflow_bypass` | `forbidden_transition` | 经 `ship` 到达 SHIPPED,且路径上 `status==PAID` 从未成立 |
| `goal_coupon_reuse` | `reach_state` | 同一 coupon key 上 `used` 两次 False→True |
| `goal_idor` | `authz_replay` | user B 对 A 的订单执行 pay/view |
| `goal_payment_bypass` | `reach_state` | `paid_amount < total` 的 PAID 订单 |
| `goal_privilege` | `authz_replay` | 非 admin 角色调用 role 修改 |
| `goal_vampi_bola` | `authz_replay` | B 的 token 读 A 的 book secret |
| `goal_vampi_pwchange` | `authz_replay` | 无旧密码改 A 的密码 |
| `goal_vampi_enum` | `reach_state` | 响应差分(枚举)——SF4 oracle 主导,规划器只出三连请求模板 |

字段:`name, vuln_class, kind, target|transition, actor(ActorSpec: role∈{any_user,other_user,admin,anonymous}, fresh_identity), max_depth=8, verify_endpoint`。

### T2 反向 BFS(2d)`planner/backward.py`
在二部 AND/OR 图(SF1 产出)上标准反向链接:
```
agenda = [(目标 state 节点, max_depth)]
while agenda:
    s, d = pop()
    for action in producers(s):                 # produces action→s
        missing = [p for p in requires(action) if not ctx.satisfied(p)]   # AND 语义
        for p in missing: push((p, d-1))        # 状态粒度 seen 去重
        ctx.bind_action(action)                 # 逆序收集
return ctx.materialize()                        # 翻转为顺序 PlanSegment
```
- **forbidden_transition 的表达**:目标=迁移终态(SHIPPED);静态图上"缺失的 requires 边"本身是漏洞信号(SF1 的 Guard/Precondition 提取直接暴露);规划器照样生成链,**真实非法性由执行时不变量 oracle 判定**(静态缺失 ≠ 运行时可达,执行才算数)
- 同动作允许重复出现(coupon_reuse 需要 redeem×2);深度上限 8(vulnshop 全部目标 ≤5 步:register→login→create→pay→ship)
- 多解:按代价(动作数×预估 ms)排序,输出 top-k(k=3)供 fuzz 做种子多样性

### T3 绑定引擎(2d)`planner/binding.py`
绑定变量三类来源:
1. **`@fresh`**:规划器自造 `sf_u{n}_{salt4}`(用户名/密码/优惠券码)——跨 epoch 幂等可重放
2. **观测提取**:`PlanStep.extract = {"$.order_id": "?oid"}` → executor 写回 `ctx.bindings`
3. **合一传播**:效应 `Order.status=CREATED(key={id:?oid})` 与前置 `Order.status==CREATED(key={id:?o})` → `?o:=?oid`,同一实体实例贯穿全链

**认证链无需特判**:`me/pay/ship ← authed_as ← login ← registered ← register` 在图上是普通路径;VAmPI 同构(`register_user → login_user(auth_token) → Bearer 头`)。

**失败恢复(两步,预算内)**:① re-observe:执行 `verify_endpoint` GET 读回重绑;② replan:以当前 ctx 为起点重跑 BFS。仍失败 → `BindingFailure` 进**覆盖缺口报告**(规划失败分类计数,论文指标)。

### T4 规划链 → fuzz 词汇表(1d)
- [ ] `ActionVocab`:端点+参数模板的枚举表(analyzer 直接产)+ 规划链模板(SF3 产出,带 `?var` 槽位)
- [ ] vocab 注入 `action_codec`(SF2):action_idx 空间 = vocab;变异只动参数区(结构由 vocab 保证)
- [ ] 对照实验设计(执行在 SF4):裸字节编码 vs vocab 编码的命中率差异

### T5 联调:直跑 + 快照跑(1d)
- 长驻目标直跑(无快照,SF2 未竟时的并行路径):验证规划-执行闭环
- 快照 epoch 跑:PlanSegment ↔ 叶 quota 对齐(驱动端请求数必须 = quota,否则 hang——已在 client 契约注明)

## 3. 测试

- 合成小图单元测试:手工构造 5 节点图,断言最短链/深度上限/循环检测
- vulnshop 5 goal:直连执行 20/20 达目标状态(bypass goal 在 vuln 变体 20/20 触发非法迁移,fixed 变体 20/20 被守卫阻断)
- VAmPI:register→login→token→me 链 20/20;BOLA 链 20/20(vuln=1)/ 阻断(vuln=0)
- 绑定:JSON 提取失败→正则回退→re-observe 三级链路单测

## 4. 验收清单

- [ ] 5 个 vulnshop GoalSpec 各生成 ≤8 步计划,直连 20/20 达成(两变体行为分化正确)
- [ ] VAmPI 认证链 + BOLA 链 20/20(vuln=1)/ 阻断(vuln=0)
- [ ] 绑定成功率 ≥95%(失败样本进覆盖缺口报告并分类)
- [ ] vocab 注入 fuzz harness 后,SMOKE 级对照:命中目标端点的请求占比显著高于裸字节(定量在 SF4)
- [ ] `stategraph.json` + 规划 trace(dot)可视化产出

## 5. 风险与回退

| 风险 | 回退 |
|---|---|
| 响应字段名漂移导致提取失败 | 双路提取 + re-observe;VAmPI golden 期人工校一次字段表 |
| 图不连通(提取遗漏导致 goal 不可达) | 覆盖缺口报告显式列出缺失边(这正是 analyzer 的质量指标);@probe 动作兜底(GET 探测 200/404 补边) |
| 非法迁移静态可判但运行时不可达(守卫在框架层) | 这不是规划器失败——Finding 置信降级,证据链保留;论文里作为 FP 面的一部分讨论 |
