# Session-First：基本块状态依赖与定向 Fuzzing

## 这份文档讲什么

这是一次针对 Session（会话）状态依赖的实现记录。

目标不是再写一个业务状态图，而是回答一个很具体的问题：

> 一段代码要走到目标基本块，到底需要 Session 里有什么值？
> 这个值可能是哪个请求写进去的？
> Fuzzer 能不能自己把这两个请求拼起来，而不是靠人提前写好答案？

实现按 SS-00 到 SS-04 分五批完成，每批单独提交、单独测试。本文记录设计、
提交、测试结果和已知问题。

---

## 1. 先看结论

- 完成了 Session AST 读写提取，能定位到源码行和真实 `BlockID`。
- 完成了 Session 访问索引和目标查询，能从目标块反推“可能需要的写入块”。
- 完成了有界的 Session 定向多请求搜索，能从一组单请求里自己拼出
  `登录 -> 访问受保护页面`，不需要预置完整序列。
- 完成了运行时 Session 观测、条件判定、检查点冷启动重放和跨客户端隔离。
- 全量回归：`731 passed, 1 skipped, 26 deselected`。
- 三臂实验里，只有单请求种子的 BASE 组 3 次都没到目标；有定向搜索和
  检查点复用的两组 3 次都到了目标。

---

## 2. 设计原则

### 2.1 基本块是唯一锚点

不新建“登录节点”“用户节点”“业务操作节点”这类图。

Session 的读和写只是挂在代码位置上的事件：

```text
app.py:165  session["uid"] = 7       写入
app.py:170  uid = session.get("uid") 读取
app.py:171  if uid is None:          条件
```

这些位置本身已经属于 `ProgramIR` 的某个基本块。分析结果里带
`BlockID`，后续 Trace、Mapper、覆盖统计都复用原有体系。

### 2.2 三种证据必须分开

| 证据 | 含义 | 能说明什么 |
|---|---|---|
| `STATIC_MAY_DEPENDENCY` | 静态分析觉得“可能有关” | 只能当候选，不能算成功 |
| `VERIFIED_SESSION_CONDITION` | 运行时从客户端 Cookie Jar 读回并确认 | 这个会话条件确实成立 |
| `TARGET_REACHED` | Trace 里真的走到了目标块 | 目标到了 |

这三层不能混。比如：

- HTTP 200 不等于目标到了。
- 写块被覆盖不等于目标到了。
- 响应里有 `Set-Cookie` 不等于会话值就是目标需要的值。
- `uid` 是 truthy 不等于 `uid` 这个 key 一定存在（值可能是别的类型，
  也可能来自别的 key）。

### 2.3 一个输入内共享客户端，输入之间隔离

一个 fuzz 输入可以是 `{"steps": [...]}`，里面多个请求共用一个
Flask test client，所以 Cookie 会自然传递。

但两个输入之间必须干净：

- 下一个输入开始时，重新建 client；
- 空 Cookie Jar；
- 不借用上一个输入留下的 Session。

这个边界由靶的 `reset_state()` 和 `--lifecycle reset` 保证。

### 2.4 不预置答案

请求池里只能放**单请求**：

```json
{"method": "POST", "path": "/authenticate", "form": {"passcode": "valid"}}
```

不能放：

```json
{"steps": [
  {"method": "POST", "path": "/authenticate", "form": {"passcode": "valid"}},
  {"method": "GET", "path": "/protected"}
]}
```

后者等于把答案直接喂给 Fuzzer。搜索器只拿到目标块和单请求池，序列要自己拼。

---

## 3. 分批做了什么

### SS-00：最小靶和输入隔离

提交：`af3c50a`

新增一个很小的 Flask 靶：

- 默认使用 Flask 签名 Cookie Session；
- `POST /authenticate`：口令正确时写 `session["uid"] = 7`；
- `GET /protected`：读 `uid`，为空返回 401，否则进入目标块；
- 还有 truthy、membership、subscript 三种守卫变体；
- `logout` 删除 `uid`，`clear` 清空 Session，`noise` 写别的 key。

配套 `fuzz_target.py`：

- 接收单个请求 dict；
- 也接收 `{"steps": [...]}`；
- 忽略输入里伪造的 `cookie` 字段；
- `reset_state()` 新建 client，清空命中记录；
- `observe_session()` 只读解码 Cookie，不触发路由。

测试：`14 passed`。

### SS-01：从源码里找 Session 读写

提交：`a183251`

核心入口：

```python
accesses = extract_session_accesses(source_dir, ir=ir)
```

每个 `SessionAccess` 记录：

- 在哪个文件、哪一行、哪一列；
- 属于哪个 `BlockID`；
- 是读、写、删除、清空，还是读改写；
- key 是常量、变量、任意 key，还是未知；
- 值表达式；
- 证据等级固定为 `STATIC_MAY`。

支持的形式包括：

```python
session["uid"] = 7                 # 只算写，不重复算读
x = session["uid"]                 # 读
x = session.get("uid")             # 读
"uid" in session                   # 判断存在
session.pop("uid", None)           # 读 + 删除
del session["uid"]                 # 删除
session.clear()                    # 影响所有 key
session.update({"uid": 7})         # 按常量 key 拆成写
session["uid"] += 1                # 读改写
session.setdefault("uid", 7)       # 读 + 写
```

也处理了：

- `from flask import session as sess`；
- `import flask; flask.session`；
- 简单局部别名；
- `session = {}` 这种局部变量遮蔽，不能误判成 Flask Session；
- 嵌套函数不重复计数；
- 动态 key 保留成 `SYMBOLIC`，不假装知道具体 key。

测试：新增 `12` 个；和旧 M1/提取测试一起跑 `37 passed`。

### SS-02：索引和目标查询

提交：`4b776ed`，后续小修 `e882460`

`SessionAccessIndex` 按 key 建索引，回答：

> 给定这个读取点，哪些写入点可能影响它？

它不是把所有写入和所有读取做笛卡尔积，而是每次只查一个读取点。

`query_session_frontier()` 做的是：

1. 从函数入口到目标块枚举路径；
2. 每条路径收集 Session 条件；
3. 不同分支的条件分开保留，不强行 AND 在一起；
4. 对每个条件查候选写入块；
5. 静态阶段统一加“同一个客户端”约束。

几个具体规则：

- `uid is None` 提前返回，目标在返回之后，推出需要 `uid EXISTS`；
- `uid == 7` 推出 `EQ 7`；
- `if uid:` 推出 `TRUTHY`，不等于 `EXISTS`；
- `"uid" in session` 推出 `EXISTS`；
- `session.clear()` 是所有 key 的潜在影响，对“uid 不存在”是候选，
  但不能拿来满足 `uid == 7`；
- 只写 `other` 的块不会匹配 `uid`；
- 动态 key 只能算 `MAY`，不能算 `MUST`；
- 无法解析的守卫放进 `unresolved`，标 `partial=True`，不猜。

测试：`14 passed`。

### SS-03：自己拼多请求序列

提交：`0752a3c`

新增 `fuzzing/session_guided.py`。它不是第二个 Fuzzer，Atheris 仍然是
唯一的变异/执行引擎。它只做一件事：在 Atheris 启动前，做一次有预算的
种子搜索，把找到的 C10 序列放进语料。

搜索器做这些事：

- 从空序列和每个单请求开始；
- 追加一个请求；
- 替换最后一步；
- 删除一步；
- 改参数值；
- 用真实 Trace 覆盖给候选打分；
- 目标块覆盖权重最高，写入块覆盖只是进展；
- 固定 seed、最大步数、最大执行次数；
- 规范化序列哈希去重；
- 每个候选开始前 `reset`，候选内部共享 Cookie Jar。

命令行开关默认关闭：

```bash
python -m stateforge.fuzzing.entry_driver ... \
  --session-guided \
  --session-target-block 'app.py::protected::173-173::block::1' \
  --session-request-pool pool.json \
  --session-max-steps 3 \
  --session-max-execs 60
```

没有可靠的 Session 观测器时，即使目标到了，也只报 `PROXY_PROGRESS`，
不会生成“已验证”的检查点。

测试：`9 passed`。

实际跑一次，搜索 15 次执行就找到了：

```text
POST /authenticate {"passcode": "valid"}
GET  /protected
```

### SS-04：运行时确认、重放和隔离

提交：`1d4a00f`，边界修复 `c859262`

运行时观测只做一件事：从当前客户端的 Cookie Jar 里读签名 Cookie，
用应用自己的 Session 接口解码。它不调用路由，不修改 Cookie，也不靠
HTTP 响应猜状态。

观测结果分三种：

- `VERIFIED_TRUE`：确认成立；
- `VERIFIED_FALSE`：确认不成立；
- `UNKNOWN`：无法确认。

`invalid` 和 `unknown` 不能当成“空会话”，只能保持 `UNKNOWN`。

每个观测都绑定：

- 客户端（`client_id`）；
- 观察点（响应后、前缀重放后等）；
- key 和裸值；
- 值类型（NUMBER / STRING / BOOLEAN / NULL / MISSING / UNKNOWN）。

检查点只从“目标到了 + 条件已验证”的结果创建。重放时：

1. 先 reset，保证是干净客户端；
2. 重放已验证前缀；
3. 重新读 Cookie，重新判定条件；
4. 可选执行后缀；
5. 后缀单独执行时不能借用前缀的会话。

如果目标到了但条件不成立，结果是 `TARGET_ONLY`，不是成功。

测试：新增 `26` 个；和 SS-00 靶测试一起 `40 passed`。

---

## 4. 怎么跑

只跑 Session 相关：

```bash
python -m pytest \
  stateforge/tests/targets/session_gate \
  stateforge/tests/state/test_session_access.py \
  stateforge/tests/state/test_session_index.py \
  stateforge/tests/state/test_session_goal_query.py \
  stateforge/tests/fuzzing/test_session_guidance.py \
  stateforge/tests/state/test_session_observation.py \
  stateforge/tests/state/test_session_replay.py \
  stateforge/tests/state/test_session_isolation.py -q
```

全量：

```bash
python -m pytest -q
```

三臂实验：

```bash
python -m stateforge.evaluation.session_experiment --seeds 1,2,3 --seconds 2
```

结果写到 `artifacts/session_first/`。

---

## 5. 测试和实验

### 5.1 测试

- Session 相关 + 靶 + 架构边界：`80 passed`
- 全量：`731 passed, 1 skipped, 26 deselected`
- 相对基线 656，新增 75 个测试，没有旧测试回归。
- 架构边界：`5 passed`
- `micro_benchmark`：静态 21 个用例的 CFG/CALL/RETURN 指标全 1.00，
  动态 5 个用例 trace accuracy 全 1.00。

### 5.2 三臂实验

同一请求池、同一 2 秒预算、同一 reset 隔离，3 个 seed：

| 组 | 目标到达 | 写入块覆盖 | Session 已验证 | 平均总执行 |
|---|---:|---:|---:|---:|
| BASE | 0/3 | 3/3 | 3/3 | 469.7 |
| SESSION-GUIDED | 3/3 | 3/3 | 3/3 | 486.3 |
| SESSION-REUSE | 3/3 | 3/3 | 3/3 | 459.7 |

BASE 组有点意思：它虽然到了登录写入块，也读回了 `uid=7`，但三次都没
走到 `/protected` 的目标块。这正好说明“写块覆盖”和“目标到达”不是一回事。

检查点 `sc-49405742647b` 冷启动重放结果是 `REPLAY_VERIFIED`，
`uid=7`；不带前缀、只跑后缀时目标不到，隔离对照通过。

---

## 6. 已知边界

- 只覆盖 Flask 默认签名 Cookie Session。服务端 Session、JWT、其他框架
  需要新的 observer，不能直接套。
- `absent` 只在当前客户端、当前观察点内可信，不能跨客户端推断。
- 静态查询可能是 `partial`。这不是失败，表示还有路径、客户端或动态 key
  没被证明。
- 检查点只适合已验证前缀；不要拿它去证明别的目标。
- 实验是 3 个 seed、2 秒、单个微型靶，只能说明功能可用，不能说明统计显著
  或通用性能优势。

---

## 7. 提交记录

| 批次 | 提交 | 内容 |
|---|---|---|
| SS-00 | `af3c50a` | 靶、C10/单请求适配、输入隔离 |
| SS-01 | `a183251` | Session AST 读写事件 |
| SS-02 | `4b776ed` | 索引和目标查询 |
| SS-02a | `e882460` | 索引 key 加入 backend |
| SS-03 | `0752a3c` | 有界定向多请求搜索 |
| SS-04 | `1d4a00f` | 运行时验证、检查点、重放、隔离 |
| SS-04a | `c859262` | 空/不存在 Session 的检查点修复 |
| 文档 | `3fe5f33` 等 | 报告和实验记录 |
