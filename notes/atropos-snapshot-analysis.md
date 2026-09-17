# Atropos 快照机制分析 → ANOTA 进程级移植(2026-09-17)

> 论文:Güler et al., "Atropos: Effective Fuzzing of Web Applications for Server-Side Vulnerabilities", USENIX Security 2024(`papers/atropos-usenix24.pdf`)。
> 实现:`exp/atropos_snapshot.py`(引擎)+ `exp/snapshot_atropos.py`(基准驱动),数据:`exp/results/atropos_snapshot_results.json`。

## 1. 论文怎么做到毫秒级(µs 级)恢复

| 机制 | 论文出处 | 内容 |
|---|---|---|
| 全系统快照 | §3.3/§4 | 构建在 Nyx 之上:VM 级快照,把 PHP 解释器、web server、DB 等**所有进程的状态原子地**冻结;快照在**所有资产初始化完成后**(含数据库)打一次 |
| 每输入恢复 | §3.3 | 每个 fuzzing input 处理完(或 **1s 超时**)即恢复到快照;保证可复现、性能不衰减 |
| Nyx 恢复速度 | §4 | **约 6,000–10,000 reloads/second**(≈100–167µs/次)——恢复 = 脏页重置,不是重建进程 |
| 每 n 输入恢复 | §3.3 | 对需要**状态累积链**的 bug(如数据库要先有上千条目),"not resetting the state after every input but only after every n inputs" |
| 多请求 input | §3.2.1 | 一个 fuzzing input 可含多个 request,单次迭代内顺序执行 |
| 剔除 HTTP | §4/§5.3 | 用 FastCGI agent(Nim)替换浏览器+web server,直接驱动解释器;"avoiding HTTP requests in the fuzzing pipeline yields performance gains" |
| Opcache + patch sleep | §4 | 避免每次恢复后重新编译 PHP;禁掉拖慢执行的 `sleep()` |
| 二阶漏洞 | §5.2 | DVWA 二阶 SQLi(先存值再触发)靠"链内不恢复"捕获 |

**为什么快**：恢复的本质是"重置脏页"而非重新初始化——初始化(解释器启动、DB 建 tables、Opcache 预热)只发生一次，之后每个 input 只付出页重置成本。

## 2. 移植到 ANOTA：Nyx 语义 → 进程级等价物

> 约束：本机无 /dev/kvm，Nyx 不可用。用 fork COW 检查点树实现同一组语义（`exp/atropos_snapshot.py`）。

| Atropos/Nyx | 进程级移植 | 状态 |
|---|---|---|
| 全资产初始化后打快照 | `spawn_root`: ANOTA 解释器 import + `app.init()`（:memory: DB）+ bind listen socket 后冻结 | ✅ |
| 每输入恢复 | 每个请求 fork 一次性叶进程，服务完 `os._exit` | ✅ |
| 恢复成本与链长无关 | **嵌套检查点树**（root→C1→C2，fd 经 SCM_RIGHTS 传递），任意深度直接 fork，不重放构建链 | ✅ |
| 每 n 输入恢复 | 叶进程 quota=n，一个 epoch 内顺序服务 n 个请求再退出 | ✅ |
| 1s 超时恢复 | `setitimer` 看门狗，超时 SIGKILL 叶，检查点无损 | ✅ |
| VM 原子性（多进程，如 MySQL） | **未覆盖**——fork 树只能捕获单进程树内状态；跨进程需 L3（CRIU/容器） | ⛔ L3 备选 |
| FastCGI 直连解释器 | 未做（保持 HTTP 真实接口，Flask/Django 需要）；HTTP 往返 ~2ms 是当前周期成本大头 | 📋 后续可做 |
| Opcache 防重编译 | 天然获得：检查点冻结时模块/pyc 已热，叶 fork 即用 | ✅ 免费 |
| patch sleep() | 未做，无害（DSVW 无 sleep；后续 fuzzing 场景可包一层） | 📋 备选 |

## 3. 实测(8核/15G,DSVW,2026-09-17)

| 实验 | 结果 |
|---|---|
| 检查点引导(= 快照创建) | 20.4 ms(解释器 import + DB init + bind,只发生一次;≈ 一次完整重启的成本被摊销到所有后续恢复) |
| 恢复成本按深度 | depth0/1/2 fork 均值 0.49 / 0.65 / 0.65 ms——**与链长无关**(Nyx 增量快照性质),~1,500–2,000 恢复/s |
| 完整周期(含请求) | ~2.6–2.8 ms,其中 HTTP 往返占大头(印证 Atropos 剔除 HTTP 的动机) |
| reset-every-n 摊销 | n=1: 3.60 ms/输入;n=10: 0.93;n=50: **0.58**(恢复成本被摊薄) |
| 超时恢复 | 0.4s 看门狗杀掉卡在 pickle-ping(~1s)的叶;之后 GET / 200,检查点健康 |
| 二阶链 | 逐输入恢复:probe 不可见;同 epoch(n=2):probe 可见——Atropos §5.2 的教训在 ANOTA 上复现 |
| **从 depth-1 恢复点跑完整 ANOTA oracle 矩阵** | **TP=11 / FN=0 / FP=0 / TN=9**,与基线/快照 harness 完全一致 |
| 检查点树内存 | root RSS 25.9MB(PSS 11.4MB),两个子检查点 PSS 各 ~6.9MB——多深度的边际内存 ~2-3MB(其余 COW 共享) |

## 4. 诚实对比与定位

- **恢复速度**:Nyx 6k–10k/s(100–167µs)vs 本实现 fork-only ~1.5k–2k/s(0.5–0.65ms),差 **~5–8×**。来源:fork 要复制页表、Nyx 只重置脏 guest 页。代价换来的是**零硬件/内核依赖**(无 KVM 也能跑,可在任意 CI/笔记本复现)。
- **保护范围**:Nyx 原子覆盖 VM 内所有进程(DB 独立进程也包含);fork 树只覆盖单进程(树)。多进程目标(Postgres 等)需 L3(CRIU/overlayfs)——与方案文档 §5 的 L1/L3 路线一致。
- **周期成本结构**:HTTP 往返(~2ms)> 恢复(~0.5ms)。若后续做 fuzzing 循环,Atropos 的"剔除 HTTP"是下一个最大杠杆(把 harness 与解释器放同进程直接调 handler)。
- 论文叙事:这组实现即方案 **M4 的"多深度检查点原型"**,且给了 VM 级 vs 进程级 vs 逻辑级三方对比的实测锚点(Nyx 100–167µs / fork 0.5–0.65ms / DB 回滚:待测)。

## 5. 复跑

```bash
cd ~/icassp/exp && ~/icassp/anota/ANOTA/ANOTA-interpreter/python snapshot_atropos.py
```

## 6. 二阶漏洞检出实验(`exp/second_order.py`,2026-09-17)

**目标改造**:`dsvw_anota.py` 新增二阶漏洞端点 `/?profile=<id>`——`?comment=<path>`(已有分支)把攻击者字符串存进 DB,`profile` 分支随后把**存储值**当文件路径读取(任意文件读)。触发请求的输入只是一个无辜整数,载荷完全驻留在数据库里——这是 Atropos §5.2 DVWA 二阶 SQLi 的同构形态(跨请求、静态污点难追)。oracle 放在**使用点**:`ANOTA_EXECUTION.BLOCK(路径必须在 web root 内, msg="ANOTA_POLICY:profile_web_root")`,sink 侧属性检查,不做跨请求污点追踪(artifact 的 TAINT hook 本就损坏,这里正好绕开)。

**三模式对比**(数据:`exp/results/second_order_results.json`):

| 模式 | store 两步 | use_benign | use_attack(二阶) | 误报 |
|---|---|---|---|---|
| 基线(长驻,状态跨请求共享) | ✅ | TN | **TP `profile_web_root`** | 无 |
| 快照·逐输入恢复(Atropos 基线策略) | 写随叶亡 | MISS(空状态) | **MISS**(返回 "No avatar set") | **无** |
| 快照·同 epoch(restore-every-n=4) | epoch 内存活 | TN | **TP `profile_web_root`** | 无 |

**结论**:二阶漏洞的检出 = **oracle(使用点注解)× 状态管理(epoch 链)** 的乘积,缺一不可——
- 逐输入恢复把链打断 → 漏检,但 oracle 不误触发(无辜输入 + 空状态 = 无 FP),快照隔离的"无害性"得到验证;
- 基线能检出但状态跨用例污染(不可复现);
- **epoch 模式 = 检出 + 隔离兼得**,这正是"ANOTA 当 oracle × Atropos 快照当地基"组合的价值证明,也是创新 3(状态感知快照:按链边界选择恢复深度)的直接实验依据。

回归:新增端点后原 20 用例矩阵基线/快照仍为 11/0/0/9,无扰动。
