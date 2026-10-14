# Phase 1 MVP 交付记录:统一状态读写索引 + 目标块状态约束推导

日期:2026-10-14。验收靶:t_state_shop(v0.4 留档"5A DB 域守卫不可见"
能力边界靶)。顾问 Phase-1/Phase-2 验收口径均达成。

## 交付一:容器 dict 库词汇(extractor)

`state/extractor.py::_extract_dict_store`:
- 写:`_DB["table"][key] = row` → `db.table` WRITE
- 读:`_DB["table"][key]` 直取 / `.get(key)` / `in` 成员 / `len()`
  → `db.table` READ(in/None 门产 EXISTS 谓词)
- 删:`del _DB["table"][key]` → DELETE
- 行变量别名:`order = _DB["orders"].get(oid)`(含三元包裹形)后,
  `order["field"]` → `db.orders.field` READ,比较语境产 EQ/NE/IN 谓词
  (值侧动态 → value=None,主体侧保留)
- ORM 事务点:`db.session.commit/flush` → WRITE,`rollback` → DELETE

命名约定:`db.<table>`(行级)/ `db.<table>.<field>`(字段级),
区别于 ORM 的 PascalCase 实体 ref。

## 交付二:读点→写点查询(index)

`StateFactIndex.writers_of(object_ref)`:
- 字段读(`db.orders.owner`)匹配:精确字段写 + 同表整行写(行写覆盖字段);
- 行/表读只匹配同表写(粒度隔离,不串表);
- 事务点(db.session.*)不按 object_ref 匹配。

t_state_shop 实测:L90 owner 读 → L79 订单行写;
L90 session.uid 读 → L72 register 会话写。**顾问 Phase-1 验收
("给定读点能找到影响它的写点")达成。**

## 交付三:目标块 → StateGoal(goal_derivation)

`state/goal_derivation.py`:
- `necessary_guards(ir, block)`:函数子图内删边可达性求必要分支边
  (支配近似);
- `derive_state_goal(ir, facts, block)`:守卫块行号区间匹配 CONTROL
  状态读点 → 约束集(StructuredPredicate),同谓词去重,证据链 file:line。

t_state_shop 实测(ship_privileged 块 `91-92`):
```
constraints = [EXISTS db.orders (L87),
               EXISTS session.uid (L90),
               EQ db.orders.owner (L90, 值=session.uid 动态→None)]
guard_blocks = [61, 64, 67, 75, 84(elif 分派链), 87(None 门), 90(归属门)]
```
与人工分析一致:register(建 uid)→ create_order(建 order, owner=uid)
→ ship_order。**顾问 Phase-2 验收("不手工指定业务状态,仅给定目标
代码位置生成候选状态条件")达成。**

## 验收测试

- `tests/state/test_dict_store_index.py`(9 例):4 历史盲区全可见、
  读→写对闭合、粒度隔离、ORM 事务点、block 绑定联动;
- `tests/state/test_goal_derivation.py`(6 例):必要守卫、约束域覆盖、
  EQ/EXISTS 算子、证据链、序列化、未知块防御;
- state+protocol 域 85/85;架构边界 5/5;全量回归见 README 冻结口径
  (451+ 新增 15 例 = 466 预期全绿)。

## 诚实边界

- 必要边为函数内支配近似,不做过程间;
- 谓词受 5A 词面约束(容器 dict 库/Session/ORM 词汇外不产约束);
- EQ 值侧动态变量 → None(Phase-3 规划期经绑定上下文补全);
- `_DICT_STORE_NAMES` 为显式白名单(_DB/DB/_db/database),
  其他容器名按需扩表。
