# Phase 5/6 交付记录:检查点复用 + 三臂对照评测

日期:2026-10-14。验收靶:stateful 三微靶(shop/chain/gate)。

## Phase 5:检查点闭环(外循环)

`solving/goal_checkpoint.py`(L3):
- **存档**:判达的 PlanExecution → GoalCheckpoint(链+目标+约束+证据,
  未判达不存档——"不能仅凭执行过就认定构造成功");
- **注入**:C10 整体种子(复用 encode_sequence_seed,消费端逐字节
  互认)→ goal_spool + corpus 双写 + 哈希去重(state_seed_injecter 惯例);
- **复用重放**:检查点 steps 直接执行(零推导),写块验证+目标判达
  双确认——检查点有效性 = 可复现。

实测:t_state_shop 检查点 gc-* 往返 3 步 3 约束;种子入语料;
重放 hits=ship_privileged 再判达。

## Phase 5 前置修复:Session 两步守卫(M1 惯用法)

`extractor._session_alias_predicates`:`x = session.get("k")` 换行后
`if x == "admin"` / `if x:` —— 读点不在守卫 test 行,曾判 NON_CONTROL
致 chain/gate 约束为空。别名传播把比较谓词归回 session.k 并标
 CONTROL(与 dict-store 行变量别名同族结构规则)。
附带谓词驱动取值(goal_to_plan):臂写 session.k 且参数名==k 且
约束含 EQ 常量 → 参数填谓词值(gate 的 role="admin",零预设)。

## Phase 6:三臂对照(goal_directed_eval)

目标块经验推导(零手挑):每 op 冷启动单请求扫描,并集外含调用
的语句块 = 状态门深块。三臂统一预算 60 请求,种子 7:

| 靶 | single_random | seq_random | goal_directed |
|---|---|---|---|
| t_state_chain | 0/1 | 1/1(60 请求) | **1/1,cost 2** |
| t_state_gate | 0/1 | 0/1 | **1/1,cost 2** |
| t_state_shop | 0/3 | 1/3(61) | **2/3,cost 8** |

解读(顾问三问):
1. **State Reach**:goal 臂 4/5;两个随机臂合计 2/5,且唯一命中
   (chain)来自"login 后随机 profile"的相邻对运气,成本高一个量级;
2. **谓词盲区**:gate 的 role==admin 类守卫随机值永不含(值集中性
   集无谓词泄露)——seq_random 0 命中即"多请求但无静态引导"消融
   的直接证据;
3. **诚实缺口**:shop 的 94-95(越权分支,owner≠uid)为否定谓词/
   双演员门,超出 MVP 主张面(条件反转见顾问 Phase-2 边界),报告
   中注记。

关键方法论修复:三臂与目标推导全部补**冷启动 reset 语义**——
否则 op 间/请求间状态泄漏使"单请求"退化为事实多步,深块被
扫描误吞、随机臂虚高(实测踩坑记录)。

## 复现

```bash
python -m stateforge.evaluation.goal_directed_eval \
    stateforge/targets/stateful --out results/goal_eval
```

## 验收测试

- Phase 5(tests/solving/test_goal_checkpoint.py,5 例):未判达不
  存档/JSON 往返/C10 格式/语料注入去重/复用重放再判达;
- Phase 6(tests/evaluation/test_goal_directed_eval.py,5 例):目标
  推导/判达与成本/冷启动零命中/成本上界/报告落盘与缺口注记。
