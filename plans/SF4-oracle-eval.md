# SF4 —— 判定层 + 评测协议:四 oracle、三基线、论文表格

> 状态:待启动 | 工期:4-5 天 | 依赖:SF2+SF3 | 上游:[00-overview.md](00-overview.md)

## 1. 目标

四类 oracle 把"到达的非法状态"变成去重的 Finding(带可重放 reproducer);评测协议产出 B0/B1/B2 三列对比与论文表格 T1-T6。

## 2. 任务分解

### T1 四 oracle(2d)`oracle/`

**① InvariantOracle(`invariant.py`)**——状态不变式
输入=提升层状态机(合法迁移矩阵,如 `CREATED→{PAID,CANCELLED}`、`PAID→SHIPPED`、`used: False→True` 单向)+ epoch 末 verify 读回观测(`GET /orders/<id>` → status)。判定:状态 ∉ 域 / `CREATED→SHIPPED` 直迁 / `used` 二次翻转 → **Finding(high)**。覆盖:workflow_bypass、coupon_reuse、payment_bypass。

**② AuthzOracle(`authz.py`)**——跨用户差分重放
对每个带实体键且 Guard(owner) 缺失的动作:A 跑全链建资源 → **同一检查点还原** → B(独立注册/登录)重放该动作(B 的 token + A 的 id)。期望 403/404;观测 2xx 且状态改变 → IDOR/BOLA/越权 **Finding(high)**。覆盖:idor、privilege、VAmPI BOLA、越权改密。
(同一检查点还原 = SF2 `freeze_child` 已验证能力,零额外成本。)

**③ AnotaBridge(`anota_bridge.py`)**——注解信道
母本 `exp/harness_dsvw.py:policies_in()`(零改动拷贝):扫描全部 Observation.body 的 `ANOTA_POLICY:(\w+)`。`app_anota.py` 变体在 SQL 拼接/文件 sink 放守卫。Finding(oracle=anota, policy=名)。

**④ ResponseDiffOracle(`response_diff.py`)**——VAmPI 补充
- 用户/密码枚举:三连(存在+对密码/存在+错密码/不存在)响应体差异 → Finding(medium)
- 过度暴露:响应字段 ⊄ analyzer `observes` 允许集(如返回 password)→ Finding
- RegexDoS:单请求 elapsed>3s 或触发看门狗 → Finding
- 限速缺失:同 epoch 30 次快速 login 全 2xx → Finding(low)
- SQLi:拼接 sink 注入探针(`' OR 1=1--`)差分 + ANOTA 桥双确认

**注册与去重(`registry.py`)**:Finding 键 = `(oracle, vuln_class, endpoint, entity_kind)`;同键合并计数,保留最优证据链(observation step_id 序列 + reproducer PlanSegment——可独立重放,reviewer 一键复验)。

### T2 评测协议(1.5d)`eval/`
**方法对比(同一预算 = 每 goal 最多 300 请求 / 10 分钟)**:
- **SF(StateForge)**:规划 + epoch 快照 + 全 oracle
- **B0 裸 Atheris**:无注解、无状态层(in-process,首 crash 即停记录)
- **B1 ANOTA+Atheris**:注解 oracle、in-process、无状态层(论文的同款基线)
- **B2' 规划+全重启**:规划器 + 每计划完整进程重启(~65ms/次,无快照)——隔离"快照层"单独贡献
- 补充列:随机逐请求 + 长驻(传统工具形态)

**指标**:① 漏洞类检出率(vulnshop 5 类×{vuln,fixed};VAmPI 8 类×{vuln=1,0})② fixed FP 数(目标 0)③ restores/s、ms/state-construction(fork/WSGI/L2 三段分解)④ requests/time-to-detection ⑤ 规划成功率/绑定失败分类 ⑥ 检查点策略消融(SF5)。

### T3 报表(1d)`eval/tables.py` + `run_eval.py`
- CLI:`run_eval.py --target {vulnshop,vampi} --variant {vuln,fixed} --engine {replay,atheris} --baseline {SF,B0,B1,B2'}` → `results/<ts>/` 混淆矩阵 JSON + markdown
- 论文表格:
  - **T1** vulnshop 检出矩阵(5 类 × 方法;预期 SF=5/5,B0≤2/5——工作流绕过/优惠券重用无规划不可达)
  - **T2** VAmPI 矩阵(8 类 × 方法,对齐 VAmPI 自带 OWASP 映射)
  - **T3** 快照性能(restore ms 按深度、±L2、restores/s、epoch ms/输入——衔接 exp/ 既有数据列)
  - **T4** 检查点策略消融(SF5 出数)
  - **T5** 规划器统计(goal 数/计划长度分布/绑定成功率/replan 次数)
  - **T6** FP 对照(vulnshop fixed + VAmPI vulnerable=0 双固定面)
- `findings.md`:每条 Finding 含计划、观测、恢复计数、reproducer 一键复跑命令

## 3. 测试

- oracle 单测:构造样本观测(合法链/非法链/跨用户)断言判定
- E2E 全场景(E1-E18,见 [e2e-test-plan.md](e2e-test-plan.md)):L3 全量本里程碑出口跑
- 复现性:同配置连跑 3 次,Findings 集合一致(允许计数抖动,键集合必须稳定)

## 4. 验收清单(SF4 出口 = 端到端第一版完整)

- [ ] **vuln 变体:5/5 类检出**(每类 ≥1 个 high Finding,带可重放 reproducer)
- [ ] **fixed 变体:0 Finding**(全 goal 套件 ×3 遍)
- [ ] VAmPI:≥2 类检出(BOLA/越权改密),vuln=0 时 0 FP
- [ ] B0/B1/B2' 同预算数字产出,T1/T3/T5/T6 表格生成
- [ ] ANOTA 桥与不变量 oracle 在 app_anota 变体上结论一致
- [ ] 全部 Findings 的 reproducer 可一键复跑

## 5. 风险与回退

| 风险 | 回退 |
|---|---|
| B1(注解+in-process)在 vulnshop 检出偏多,削弱叙事 | 这是真实结果:注解 oracle 单独也能抓 sink 类;叙事重心放在**状态类**(workflow/coupon)B1 结构性抓不到——T1 按类分列正是为此 |
| ResponseDiff 误报(正常业务差异) | 三连差分要求控制变量(同 endpoint 同方法);low/medium 置信不影响主指标;T6 FP 面覆盖 |
| 预算内 B0 偶发命中状态类漏洞 | 报告为运气命中(带 requests-to-detection);多 seed 取中位数,不藏负结果 |
