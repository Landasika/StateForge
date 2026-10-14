# Phase 4 交付记录:定向状态构造(计划→执行→验证→重规划)

日期:2026-10-14。验收靶:t_state_shop。顾问 Phase-4 口径达成:
"对给定 StateGoal,自动产生满足它的请求链和执行证据"。

## 内循环闭环(Plan → Execute → Verify)

```
目标块 ship_privileged(91-92,v0.4 零注入边界靶)
  → 三步链 + 参数绑定(结构提取,零预设名)
  → 逐步执行 target({"json": {...}})(C10 单步协议,进程内状态累积)
  → 每步 TraceSession 轨迹 → map_trace → ICFG 块覆盖
     判据 = 预期写块被真实执行(register→71-74 块,create_order→78-83 块)
  → 终步目标块覆盖 + HITS 双证据
实测:HITS = [register_ok, order_created, ship_privileged],全步 ok。
```

## 交付

| 模块 | 内容 |
|---|---|
| `goal_to_plan.arm_params` | 参数 AST 结构提取:`body = x.get("json")` 链上 `.get("常量")`/下标形;**可选覆盖参数不填**(`body.get(k) or fallback` 形态——中性值会抢占会话 fallback 路径,ship 判达的关键修复);参数名全来自源码常量,值=非空中性串(守卫结构安全默认) |
| `runtime/plan_executor.py`(L2,向下引用 state ✓) | `execute_plan`(逐步执行+写块验证)/ `execute_plan_for_block`(一条龙+整链重规划);`reset_state` 契约复位;异常捕获入 blocking 证据 |

## 验收测试(7 例)

- 正向:三步全 ok 判达(HITS+块覆盖双证据)、逐步写块验证、
  参数结构提取(register={op,user,password};ship 无 order);
- 负向:password 空串 → register 写块未覆盖 → blocking="guard
  rejected" 如实报告;残链(ship 单步)失败检测;
- 重规划:首链残(mock 规划缺陷)→ 重推导 → 判达恢复 + 失败步留痕。

## 诚实边界

- 执行面 = 微靶族(C10 单步协议,reset_state 契约);apps20 真实应用
  执行需 HTTP harness + 认证引导(Phase 5 集成);
- 重规划 MVP = 整链重推导重试(同推导同参数时对参数错误无效,如实
  报告);步级子链拼接与参数级修正留 Phase-5;
- 参数值统一非空字符串;数值/枚举约束参数接 branch_solver/LLM 通道
  留后续(谓词驱动取值的完整版)。
