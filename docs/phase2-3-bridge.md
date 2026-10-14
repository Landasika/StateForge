# Phase 2/3 桥接交付记录:目标块 → 自动多步构造链

日期:2026-10-14。验收靶:t_state_shop。顾问 Phase-3 口径达成:
"能自动生成 create→pay→ship 等多步序列,而不是依赖预设模板"。

## 交付:goal_to_plan.py(state 域)

一条龙 `plan_for_block(ir, facts, target_block)`:
```
未覆盖目标块
  → derive_state_goal(必要守卫 CONTROL 读点 → 约束集)
  → dispatch_arms(elif op=="x" 分派臂行区间 → action 名)
  → synthesize_appmodel(事实按臂聚类:
      CONTROL 读→Precondition,WRITE→Effect,
      整行写字典键→字段级 Effect)
  → build_sstg(produces/requires 边)
  → plan_backward(反向链:择优生产者)
  → 补终结动作(目标块所在臂 = 消费状态命中深块的操作)
```

## 值形约定(链闭合关键)

- EXISTS/truthy 前置 → atom `<var>=True`;整行写/Session 写
  Effect value="True" 对应;
- EQ 动态值前置 → atom `<var>=*`;整行写的**字典键** Effect
  value="*" 对应(`_DB["orders"][o] = {"owner": uid, ...}` 的
  "owner" 键使归属约束可回溯到订单行写)。

## t_state_shop 实测

```
plan steps = [POST op=register, POST op=create_order, POST op=ship_order]
notes      = [final-action: ship_order(目标块所在臂,消费状态命中深块)]
```

- 全程零模板:无 step_template、无 _JSON_FILL 预填、无认证链补全
  (兼容层在合成模型上无触发结构);
- 链序与状态依赖一致:register(产 session.uid)→ create_order
  (需 uid,产 db.orders.owner)→ ship_order(需 order 归属);
- 深块 ship_privileged(单请求词法不可达,v0.4 零注入的边界靶)
  现在有机器可读的构造路径。

## 验收测试

`tests/state/test_goal_to_plan.py`(6 例):分派臂发现、三步链精确序、
依赖序、终结动作注记、合成模型形态(session.uid 生产者/owner 门)、
目标块选择正确性。

## 诚实边界

- PlanStep 参数槽为空(MVP 交付链序;请求参数绑定/op 体 JSON 形态
  是 Phase-4 执行层的工作);
- dispatch_arms 仅识别 `op == "常量"` 分派惯用法(微靶族);
  Flask/Django 路由靶走 patterns.analyze 原生 AppModel,不经此桥;
- 桥接产物是合成 AppModel(framework="synthetic",词表扩充),
  不与 patterns 产出的语义模型混用。
