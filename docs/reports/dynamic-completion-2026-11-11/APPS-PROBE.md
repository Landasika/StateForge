# 10 应用启动与状态隔离探测（2026-11-12）

为 v0.3.1 计划 M0 的前置验证：在写 registry、真值和规划器之前，先确认
**这 10 个真实应用在今天的环境里到底能不能被驱动**，以及**同进程重跑是否
等价于重来一次**。

两个问题都只需只读探测，不涉及任何算法改动。

## 结论

- **启动：10/10 可用。** 312 条路由，287 条返回 2xx/3xx（92.0%），
  零 5xx、零 import 失败、零超时。
- **隔离：10/10 可用于多步实验**，但**只有 6 个的 `smoke()` 是幂等的**。
  另外 4 个的漂移都有明确、可解释的原因，不是隐藏的状态泄漏。

关键区分：**"重跑结果不同" ≠ "状态泄漏"**。前四个判定类别都是设计使然，
最后一个类别才是真正需要修的问题（本次为 0 个）。

## 启动探测

`probe_apps_launch.py`，每个应用在**自己的 venv** 里以子进程方式 import
`targets/apps20/<slug>/harness.py` 并调用统一的 `smoke()`。

| 应用 | 路由 | 2xx/3xx | 4xx | 5xx | 耗时 |
|---|---|---|---|---|---|
| ctfd | 28 | 24 | 4 | 0 | 8.4s |
| linkding | 39 | 38 | 1 | 0 | 5.1s |
| fava | 30 | 28 | 2 | 0 | 0.8s |
| wger | 40 | 40 | 0 | 0 | 35.9s |
| microblog | 28 | 25 | 3 | 0 | 1.8s |
| octoprint | 32 | 29 | 3 | 0 | 5.4s |
| spirit | 24 | 23 | 1 | 0 | 3.8s |
| zinnia | 38 | 36 | 2 | 0 | 5.4s |
| mezzanine | 29 | 26 | 3 | 0 | 4.1s |
| oscar | 24 | 18 | 6 | 0 | 15.4s |

4xx 全部是**设计好的负例**，不是环境故障：

```
/user/ghost=404            故意访问不存在的用户
/nonexistent-page/=404     故意访问不存在的页面
/comment/upload/=405       故意用 GET 打 POST-only 端点
/api/printer=409           OctoPrint 打印机未连接（真实业务状态）
```

最后一条有额外价值：`409` 说明 harness 进入了「打印机离线」这个业务状态，
本身就是状态门控目标的候选。

`oscar` 最弱（18/24 = 75%），计划里已把它列为风险项；但它是**部分可用**，
不是 `ENDPOINT_UNMAPPED`。

## 状态隔离探测

`probe_state_isolation.py`，同一进程内连续调用两次 `smoke()` 并逐路由比较；
再与一个冷进程的结果对比。

### 判定类别

- `ISOLATED`（6 个：ctfd / fava / octoprint / spirit / zinnia / oscar）——
  两次结果完全相同，重跑即重来。
- `LOGOUT_BY_DESIGN`（2 个：microblog / mezzanine）——harness 的 `ROUTES`
  末尾**故意**放了一条 logout（注释写着「最后:清会话」）。第二次跑开始时用
  户已登出，于是受保护路由 200→302、登录/注册页 302→200。
- `SELF_DELETING_LOGOUT`（1 个：wger）——wger 在 `user_logged_out` 信号里
  **删除临时访客用户**（`wger/core/signals.py::delete_temporary_user_on_logout`）。
  第二次 smoke 的 logout 操作的是第一次已被删除的行，抛
  `ValueError: User object can't be deleted because its id attribute is set to None`。
- `VOLATILE_IDS`（1 个：linkding）——书签 hash 每次进程启动随机生成，两次冷
  进程的路径不同但路由集合相同。没有泄漏，只是 ID 随机。
- `RAISES_ON_REPEAT`——本次 **0 个**。这一类才是真正需要修的：smoke 无法重复
  就等于无法测量。

### 为什么必须区分这几类

最初的判定把 microblog 的 14 条漂移报成 `STATE_LEAK`，那是**错的**。
`ROUTES` 里最后一行就是：

```python
("GET", "/auth/logout"),   # 最后:清会话
```

把设计行为报成缺陷，会让 M0 的「执行隔离」验收项凭空多出 4 个阻塞项，
而这 4 个其实只需在每次运行前重新登录——campaign 本来就会做这件事。

判定过程中还修掉了一个真实 bug：取路径时用了 `diff.split(" ", 1)[0]`，
拿到的是 HTTP 方法 `"GET"` 而不是 URL，导致「登录页」判断从未生效。

## 对 M0 的影响

1. **分母是 10，不是 6。** 上一轮我担心「可能只有 5-6 个能跑」，不成立。
2. **进程内 harness 的 reset 策略要具体化。** 这 10 个都是
   `APP.test_client()` 而非独立服务器，所以「重置」= 重建 app 对象 /
   重新登录 / 重建 fixture，而不是重启进程。计划里「固定执行隔离」需要
   落成每个 app 的具体做法：
   - microblog / mezzanine：每次运行前重新 POST /login
   - wger：每次运行前重建临时用户（原用户已被 logout 删除）
   - linkding：从 harness 取运行时生成的 ID，不能用捕获的常量
3. **`oscar` 需要单独确认**：6 条 4xx 里包含 `/shipping-address/...`，
   如果地址是下单流程的必要一步，它的多步链会比其他 app 更长。

## 复现

```bash
# 启动探测
python docs/reports/dynamic-completion-2026-11-11/probe_apps_launch.py

# 状态隔离探测
python docs/reports/dynamic-completion-2026-11-11/probe_state_isolation.py
```

两侧都把原始结果写成 JSON（`apps_launch_probe.json`、
`state_isolation_probe.json`），报告中的数字可从这两个文件复算。
