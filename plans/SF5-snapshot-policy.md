# SF5 —— 创新 3:状态感知的检查点调度(评分制)+ 多深度检查点

> 状态:待启动 | 工期:5-6 天 | 依赖:SF4 | 上游:[00-overview.md](00-overview.md) · 理论依据:`notes/atropos-snapshot-analysis.md`(多深度检查点原型已验证:恢复与深度无关)

## 1. 目标

把"在哪里打检查点"从拍脑袋(every-request / 固定登录后)变成**可评分、可消融的调度策略**——论文创新点 3。同时完成 VAmPI 的多深度接入。

## 2. 任务分解

### T1 检查点评分(1.5d)`snapshot/policy.py`
```
Score(s) = α·security_relevance(s)      # 状态原子距目标非法迁移的图距离(负相关)
         + β·hit_rate(s)               # 该前缀状态被多少 goal 复用(重放需求)
         + γ·(1/(1+rebuild_cost(s)))   # 重建成本 = 动作数 × 实测 ms
```
- 输入:状态图(SF1)+ 运行统计(RunStats:各前缀复用计数、重建耗时实测回填)
- 输出:epoch 间隙的调度决策——`freeze_child(name, build)` 与否
- α/β/γ 网格搜索(vulnshop 上调参,VAmPI 上验证,防过拟合叙事)

### T2 四策略消融(2d)
| 策略 | 语义 |
|---|---|
| `root-only` | 只用 depth-0,每次从根重放(对照下界) |
| `static-auth` | 登录后冻结(Atropos 风格固定点) |
| `random` | 等概率随机冻结 |
| `scored` | 评分制( ours) |

指标:**time-to-detection(每漏洞类)**、总 restore 次数、检查点树 PSS(内存代价,SF2 已有 `_pss_kb` 通道)。

### T3 多深度检查点链(1.5d)
- 在"登录后 / 建资源后 / 支付后"打嵌套检查点(引擎 `freeze_child` + SCM_RIGHTS 已验证,depth 无关恢复 ~0.65ms)
- **目标状态复用**:SF3 规划的多 goal 共享前缀(如都要 register→login→create)——只构造一次,之后从 depth-k 直接恢复,不同 goal 分叉
- 撤销探索:试错分支(规划失败/fuzz 死路)恢复到最近检查点而非根

### T4 VAmPI 深度接入(1d)
- VAmPI 的 goal 套件全量接入(BOLA 链的"A 建资源后"检查点、改密链的"A 登录后"检查点)
- L2 模板与 VAmPI SQLAlchemy 文件 DB 的多深度组合测试(SQLAlchemy 连接池 + 多代模板)

## 3. 实验(产论文 T4 表)

- 设计:4 策略 × {vulnshop 5 goal, VAmPI 3 goal} × 3 seed;预算同 SF4
- 假设:`scored` 的 mean-TTD ≤ `root-only` 的 70%,检查点数相对 `every-request` 降 ≥50% 且覆盖率(检出集合)不降;多深度复用使前缀重放减少 ≥70%
- **负结果也报告**(诚实叙事,同 m1 报告风格):至少给出 score-成本-收益曲线

## 4. 验收清单

- [ ] 评分公式实现 + 网格搜索记录(参数表存 `results/sf5/`)
- [ ] 四策略消融表(T4)生成,含 PSS 内存列
- [ ] 多深度:≥3 深度检查点树,共享前缀重放减少 ≥70%(对照 root-only)
- [ ] VAmPI 多深度 + L2 组合正确性(零漂移复测)
- [ ] SF2 覆盖反馈 spike 结论对调度的影响评估(若为每 epoch 反馈,scored 策略的信息量如何补偿)

## 5. 风险与回退

| 风险 | 回退 |
|---|---|
| 评分调参过拟合 vulnshop | 留一验证:VAmPI 上验证参数;报告两套数字 |
| 检查点树内存膨胀(深度×数量) | 实测 SF2 数据:子检查点 PSS ~6.9MB,边际 ~2-3MB;LRU 淘汰低分检查点 |
| 多深度与 fuzz epoch 语义冲突(fuzz 不知规划前缀) | fuzz 模式固定用 `static-auth`;scored 仅用于 replay/goal 模式——两引擎解耦 |
