# StateForge 端到端测试计划(四层)

> 版本:v1.0(2026-09-17)| 适用:SF0-SF6 全周期 | 执行入口见 §5

## 0. 原则

1. **每个植入漏洞一条剧本**(固定格式:前置状态 → 动作序列 → 期望观测 → 命中 oracle → 通过标准);
2. **每个 vuln 场景必有 fixed 对照**(不可达 = 通过),FP 与 TP 同等是一等公民;
3. **回归红线**:`exp/` 论文实验(DSVW 11/0/0/9 + 快照基准)任何里程碑后不得回归(exp/ 冻结,理论上不受影响,仍要跑);
4. **可复现性**:同配置连跑 3 次,Finding 键集合必须稳定(fuzz 场景允许计数抖动,但"检出/未检出"必须一致)。

## 1. L1 单元 / golden 层(每里程碑合并前,秒-分钟级)

| 测试 | 断言 | 归属 |
|---|---|---|
| U1 AST golden(vulnshop) | 端点 15/15,effects/preconditions 与 golden diff ≥90%,遗漏显式列出 | SF1 |
| U2 AST golden(VAmPI) | openapi 路由覆盖 100%,效应人工核对 ≥70% | SF1 |
| U3 pattern 规则单测 | 每条 AST 模式一个最小用例(合成片段→期望产出) | SF1 |
| U4 L2 恢复一致性 | 模板 sha 往返一致;journal/wal 清理;20 epoch 零漂移 | SF2 |
| U5 引擎恢复时延 | 纯 fork ≥1200/s;带 L2 ≥500/s(DB≤1MB) | SF2 |
| U6 规划器合成图 | 5 节点手工图:最短链/深度上限/循环检测/coupon×2 重复动作 | SF3 |
| U7 绑定链路 | JSON 提取→正则回退→re-observe 三级 | SF3 |
| U8 oracle 判定 | 构造样本观测(合法/非法/跨用户)各断言 | SF4 |
| U9 LLM schema | 非法输出→重试→启发式降级;缓存二次零 token | SF6 |
| U10 Atheris 门禁 | SF0 T3 冒烟脚本(30s 内 crash artifact 产出) | SF0(已过) |

## 2. L2 组件集成层

| 测试 | 断言 | 归属 |
|---|---|---|
| I1 epoch 隔离复演 | vulnshop 上三模式(baseline/逐输入/epoch)行为与 `exp/second_order.py` 结论一致(写随叶亡/链内可见) | SF2 |
| I2 检查点健康 | RegexDoS payload 触发看门狗(0.4-2s)后,检查点服务正常 | SF2 |
| I3 跨用户还原 | A 建资源后 freeze_child → B 从该检查点重放 → 观测隔离正确 | SF4 |
| I4 GUARDS 切换 | 同一检查点树下重建根,vuln/fixed 两变体行为分化 | SF2 |
| I5 VAmPI+L2 | SQLAlchemy 文件 DB 多代模板组合零漂移 | SF5 |
| I6 覆盖反馈 | spike 结论对应的模式(snapshot 反馈可用性/粒度)按 SF2 §5 结论断言 | SF2 |

## 3. L3 端到端场景层(E1-E18;SF4 出口全量,SF5/SF6 增量)

| # | 场景 | 靶场/变体 | 动作序列(要点) | 期望 | oracle |
|---|---|---|---|---|---|
| E1 | workflow bypass | vulnshop/vuln | register→login→create→**ship**(跳过 pay) | SHIPPED-without-PAID 达成,`CREATED→SHIPPED` 直迁被记录 | invariant(high) |
| E2 | coupon reuse | vulnshop/vuln | 建单(coupon)→ 建单(同 coupon) | 同一 coupon 两次成功 redeem,`used` 二次翻转 | invariant(high) |
| E3 | IDOR | vulnshop/vuln | A 建单 → B login → B pay/view A 的单 | B 的请求 2xx 且状态改变 | authz(high) |
| E4 | payment bypass | vulnshop/vuln | create(total=100)→ pay(amount=1) | PAID 且 `paid_amount<total` | invariant(high) |
| E5 | 越权 admin | vulnshop/vuln | 普通用户 login → POST /admin/users/<id>/role | 2xx 且角色被改 | authz(high) |
| E6-E10 | **fixed 对照** | vulnshop/**fixed** | E1-E5 同剧本 | **全部不可达(4xx/拒绝),0 Finding** | 全部 |
| E11 | BOLA | VAmPI/vuln=1 | A register→login→post book(secret)→B login→GET A 的 book | B 读到 secret(2xx) | authz(+response_diff) |
| E12 | 越权改密 | VAmPI/vuln=1 | B login → PUT A 的 password(无旧密码) | 修改成功(2xx) | authz(high) |
| E13-E14 | **vuln=0 对照** | VAmPI/vuln=0 | E11/E12 同剧本 | 不可达,0 Finding | 全部 |
| E15 | 二阶复演 | vulnshop(存→用) | 三模式各跑 comment 类存→用链 | 与 DSVW 二阶实验结论一致(baseline=DETECT/per-input=MISS+0FP/epoch=DETECT) | invariant |
| E16 | **回归红线** | exp/ 论文实验 | `harness_dsvw.py` + `snapshot_dsvw.py` + `snapshot_atropos.py` | 矩阵 11/0/0/9;fork ~0.5ms;epoch 摊销曲线不变 | — |
| E17 | Atheris 冒烟 | vulnshop/vuln,B1 配置 | in-process 限时 60s | libFuzzer 记录 ≥1 注解违规 crash | anota |
| E18 | Atheris+快照 | vulnshop/vuln,B2 配置 | snapshot 模式同预算 | 违规可检出;复跑两次结果一致(隔离+可复现) | anota+invariant |

**通过标准**:E1-E5、E11-E12、E17、E18 = 检出;E6-E10、E13-E14 = 0 FP;E15 = 三模式分化;E16 = 数字不回归。SF4 出口要求全绿;之后每里程碑合并前 E16 必跑。

## 4. L4 评测驱动层

```bash
# 全场景自动编排(SF4 起可用)
python -m stateforge.eval.run_eval --target vulnshop --variant vuln   # E1-E5,E15,E17,E18
python -m stateforge.eval.run_eval --target vulnshop --variant fixed  # E6-E10(0 FP)
python -m stateforge.eval.run_eval --target vampi   --variant vuln    # E11,E12
python -m stateforge.eval.run_eval --target vampi   --variant fixed   # E13,E14
python -m stateforge.eval.run_eval --baseline B0|B1|B2' --engine atheris|replay  # 基线列
```
- 产物:`stateforge/results/<ts>/matrix.json` + `matrix.md` + `findings.md`(每条含 reproducer 复跑命令)
- 冒烟模式 `SMOKE=1`:每 goal 预算缩至 30s,合并前跑;全量在 SF4/SF5/SF6 出口跑

## 5. 门禁策略(什么时候必须跑什么)

| 时机 | 必跑 |
|---|---|
| 每个 commit(本地 hook 可选) | L1 快速子集(U3/U6/U8) |
| 每个里程碑合并前 | L1 全量 + L2 全量 + E16 + SMOKE=1 L3 |
| SF4/SF5/SF6 出口 | L3 全量(E1-E18)+ L4 全基线 ×3 seed |
| 任何 snapshot/ 引擎改动 | I1-I6 + U5 + E16 |

## 6. 已知测试债(诚实记录)

- fuzz 场景(E17/E18)受 Atheris 随机性影响:断言"检出≥1"而非确定性复现;3 seed 中位数入报告
- VAmPI RegexDoS/限速类依赖 oracle 阈值(3s/30 次),阈值敏感度在 SF4 报告中给敏感性曲线
- 覆盖反馈若走每 epoch 降级路线(B),E18 的复现性断言粒度从"每输入"放宽到"每 epoch"
