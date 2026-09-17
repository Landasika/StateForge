# 技术方案:ANOTA × 快照式 Web 漏洞检测复现实验(存档)

> **存档说明**:本文为用户 2026-09-17 版技术方案原文(会话中提供,盘上原先无此文档)。这是 StateForge 计划(`plans/00-overview.md` 等)的上游依据,此处存档以保可追溯。原状态"待确认"已由后续工作 supersede:M1/M4 已完成并复现(`notes/m1-results-dsvw.md`、`notes/atropos-snapshot-analysis.md`),后续开发按 `plans/` 各 SF 文档执行。
> 版本:v1.0(2026-09-17)
> 关联文档:`innovation2-state-construction-algorithm.md`(创新 2 算法设计;**注:该文件未随本环境迁移,内容待用户提供后补存**)

## 1. 目标与定位

**做这件事的原因**:三创新点方案(LLM 状态语义标注 / 目标状态反向构造 / 状态感知快照)的可行性验证地基。本实验回答三个问题:

1. ANOTA 的 artifact 能否在我们的环境复现论文的 35 漏洞套件结果(其消融实验:35/35 检出、0 误报)?
2. Atropos 式"每个 testcase 从干净快照出发"的机制,换成我们的快照实现后,恢复开销和状态隔离性实测如何?
3. 两者拼起来(注解 oracle + 快照重置)是否构成后续创新点的可用实验底座?

**在论文里的角色**:Phase 0(复现)+ Phase 1(快照基线)。创新 1(LLM 标注)的 gold standard 就来自本实验手写的注解。

## 2. 环境与关键约束(2026-09-17 新环境已核实更新)

| 项 | 状态 | 影响 |
|---|---|---|
| OS / 硬件 | Ubuntu 24.04, 8 核 / 15G RAM / 109G 空闲 | 编译可行,跳过 PGO/LTO 提速 |
| **/dev/kvm** | **不存在**(宿主未开嵌套虚拟化) | **Nyx(QEMU/KVM-Nyx)不可用** → Atropos 快照走进程级等价实现 |
| rustc / clang | clang 已装(SF0);rustc 缺失 | eBPF syscall 模块(Aya/nightly)跳过;LLVM 插桩跳过 |
| ANOTA 构建 | ✅ 成功(CPython 3.10.13 fork,gcc 13.3,新环境重建) | Flask 栈 + atheris 2.0.12 已装入 |
| ANOTA 自检 | WATCH/EXECUTION ✅ 全过;**TAINT ❌ 有 bug** | 35 套件以 WATCH/EXECUTION 为主,不受阻 |
| ANOTA_SYSCALL | artifact 版为仅日志、需 Rust 守护进程 | 文件类策略改用 `ANOTA_EXECUTION.BLOCK` 守卫风格表达(语义等价) |
| 35 套件靶场 | artifact 不附带 | 自行获取:DSVW ✅ / VAmPI ✅(待接入)/ Pygoat(待取)/ OWASP Vulnerable Flask App(待取) |

## 3. 系统架构

```
┌────────────────────────────────────────────────────┐
│ harness(请求驱动器:良性集 + 攻击集,分类判定)      │
└──────────────┬─────────────────────────────────────┘
               │ HTTP
┌──────────────▼─────────────────────────────────────┐
│ 被测应用(注解版源码)+ ANOTA 解释器                   │
│  · oracle:ANOTA_EXECUTION.BLOCK / ANOTA_WATCH       │
│    违规 → RuntimeError → 进入检测信道                │
│  · 注解风格:msg="ANOTA_POLICY:<名>" 便于归因        │
└──────────────┬─────────────────────────────────────┘
               │ 检测信道(应用无关)
┌──────────────▼─────────────────────────────────────┐
│ 判定:响应体含 "ANOTA" 即违规命中                     │
│(DSVW 类:异常 traceback 进 500 响应体;             │
│  Flask 类:harness 挂 Flask errorhandler 捕获)       │
└────────────────────────────────────────────────────┘
```

两种运行模式对比:

| | 基线模式(长驻服务) | **快照模式(每 testcase 恢复)** |
|---|---|---|
| 实现 | subprocess 启动,持续服务 | 父进程 = 检查点(DB 初始化完成、listen socket 已绑定);每请求 `fork()` COW 子进程服务一个请求后 `_exit` |
| 状态语义 | 请求间共享脏状态 | **每个 testcase 从快照态出发**(Atropos 语义) |
| DB 隔离 | 无(`:memory:` 全局共享) | fork 复制 → 子进程写不可见,天然快照隔离 |

## 4. 实验设计

### 4.1 套件构成(对齐论文 Appendix B 的 35 漏洞分布)

| 靶场 | 栈 | 论文漏洞数 | 状态 |
|---|---|---|---|
| DSVW | 纯 stdlib 单文件 | 10 | ✅ 端到端跑通(注解 11 策略,含 lxml 解禁的 XXE/XPath) |
| VAmPI | Flask(connexion)+ OpenAPI 3 | 4 | SF0-SF5 接入(StateForge 双线靶场) |
| OWASP Vulnerable Flask App | Flask | 5 | 待获取+注解(可选,地位被 vulnshop 部分取代) |
| Pygoat | Django | 16 | 待注解(v2/L3 阶段;L2 模板 DB 恢复) |

### 4.2 判定指标

- **检测**:每靶场 TP/FP/TN/FN,宏平均 precision/recall,对比论文 35/35 + 0 FP
- **快照**:① 每 testcase 恢复开销(fork/serve/reap 分项);② 对照组:完整进程重启;③ 父进程 RSS 稳定性(状态污染免疫);④ 吞吐(testcase/s)
- **成本**:每靶场注解数、改动行数(创新 1 的"人工 gold standard"成本基线)

### 4.3 已完成的运行(DSVW,双模式;2026-09-17 新环境复现)

| 模式 | 结果 | 开销 |
|---|---|---|
| 基线 | TP=11 FN=0 FP=0 TN=9 | 完整重启 ~65ms |
| 快照 | TP=11 FN=0 FP=0 TN=9 | fork 0.20-0.48ms + reap ~0.6ms,**恢复(fork+reap)中位 0.84ms**(≈ 重启的 1/77) |
| 污染检查 | — | 父进程 RSS 28928→29056 kB(打完全矩阵+DoS 后) |
| 多深度(Atropos 移植) | 矩阵同 11/0/0/9(depth-1 恢复点) | 恢复与深度无关 0.49-0.65ms;epoch n=1→3.6ms/输入,n=50→0.58 |
| 二阶漏洞 | baseline=DETECT / 逐输入=MISS(0 FP)/ epoch=DETECT | **检出 = oracle × 状态链,缺一不可** |

## 5. 快照机制:三级实现路线

无 KVM 是硬约束,按侵入度递进:

| 级别 | 机制 | 状态 | 适用 |
|---|---|---|---|
| L1(当前) | **pre-fork 进程快照**:父进程冻结在检查点态,每 testcase fork COW 子进程 | ✅ DSVW 跑通 + 多深度检查点树 | 进程内状态 + `:memory:` DB |
| L2 | fork + **文件 DB 恢复**:每 testcase 从模板 DB 文件恢复 | SF2 实装 | 文件型 SQLite(vulnshop/VAmPI/Pygoat) |
| L3 | **容器级**:overlayfs 快照 / CRIU / ZFS clone | 备选 | 多进程服务(Postgres 等)、无法 fork 的目标 |

> 论文表述注意:我们的快照是**进程级**(语义与 Atropos 一致,开销比 Nyx 的 VM 快照更低层但保护范围更小);Nyx 数字只能引文献,不能声称复现。可写的对比点:VM 级 vs 进程级 vs 逻辑级(DB 回滚)的恢复开销/保护范围权衡(已有实测锚点:Nyx 100-167µs(文献)/ fork 0.5-0.65ms(实测)/ L2 待测)。

## 6. 里程碑(原版;M1/M4 已完成,后续并入 plans/ 的 SF0-SF6)

- **M1(已完成)**:环境核实、ANOTA 构建、自检、DSVW 双模式端到端
- **M4(已完成,提前)**:快照基准专项 + **多深度检查点原型**(在"登录后/建资源后"打多个 fork 检查点 = 创新 3 状态池的雏形);Atropos 机制移植(每输入恢复/每 n 输入/超时恢复/多深度)
- **M2**:VAmPI 注解与双模式 → 并入 SF0/SF4
- **M3**:Pygoat(Django):L2 → 并入 v2
- **M5**:创新 1 实验协议 → 即 SF6

## 7. 风险与已发现问题(历史记录,带最终处置)

1. **ANOTA TAINT hook 的 C 层 bug**(artifact 自带测试 40 起崩溃):`_PyEval_EvalFrameDefault returned a result with an exception set`。**处置:绕过不修**(套件用 WATCH/EXECUTION;taunt 类检测由不变式/差分 oracle 替代)。
2. **pickle RCE 副作用先行**:载荷在 `pickle.loads` 内部已执行(ping 真实发包后才命中)。诚实记录:EXECUTION 守卫对"loads 即 RCE"类是**检测器不是阻止器**;阻止需 WATCH 风格入口拦截(后续实验项)。
3. **lxml**:新环境已装(6.1.3),XXE/XPath case 已解禁。
4. **注解位置语义**:注解必须在 try 覆盖区内,否则 RuntimeError 炸连接而非进检测信道(已踩坑:`finally` 块里的注解导致 RemoteDisconnected)。已写入注解规范。
5. **fork-per-request 与 ThreadingMixIn 冲突**:快照模式一律同步 server;子进程退出前 join 在途工作。
6. Pygoat(Django,16 漏洞)工作量最大且 Django 的 file-backed SQLite 跨 fork 写安全需要 L2 模板恢复 → v2 阶段。

## 8. 目录与产物(新环境,2026-09-17)

```
/home/landasika/icassp/
├── anota/                  # ANOTA artifact(经 gh-proxy 镜像获取)+ 已构建解释器
│   └── ANOTA/ANOTA-interpreter/python   # 3.10.13 fork;flask 栈 + atheris 2.0.12 已装
├── targets/dsvw/           # DSVW 靶场 + fixture
├── exp/                    # 冻结的论文实验(dsvw_anota / harness / snapshot 三件套 +
│   │                       #   atropos_snapshot 引擎 / second_order 二阶实验)
│   └── results/*.json      # 全部实测数据
├── papers/                 # atropos-usenix24.pdf
├── notes/                  # env-rebuild / m1-results / atropos-snapshot-analysis
├── plans/                  # StateForge 开发计划(本文件夹)
└── stateforge/             # SF0 起开发的框架本体
```
