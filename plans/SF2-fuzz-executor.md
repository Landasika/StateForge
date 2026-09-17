# SF2 —— Fuzz 执行 + 快照层:Atheris harness、双模式、L2、覆盖反馈 spike

> 状态:待启动 | 工期:6-8 天 | 依赖:SF0(硬门禁已过),SF1 并行可 | 上游:[00-overview.md](00-overview.md)

## 1. 目标

把三块拼成整体:① Atheris fuzz 执行器(字节→动作→执行→oracle,双模式);② 规划链确定性重放执行器(SF3 依赖,先做);③ 快照层工程化(fork 引擎重构 + L2 文件 DB 模板恢复)。**跨 fork 覆盖反馈 spike** 是本里程碑的攻坚点。

## 2. 任务分解

### T1 snapshot/engine.py(1.5d)—— fork 引擎改造(母本 `exp/atropos_snapshot.py`,~80 行 diff)
- [ ] `_send/_recv` 原语原样拷贝到 `protocol.py`(AF_UNIX socketpair/文件 socket 均字节兼容)
- [ ] `spawn_root` → `supervisor.py: spawn_supervisor(sock_path)`:**AF_UNIX bind/listen/accept** 替代 socketpair(驱动/监督两进程族);监督进程 = ANOTA python `-m stateforge.snapshot.supervisor --target <spec>`
- [ ] `CountingHTTPServer` → `wsgi_bridge.CountingWSGIServer(werkzeug BaseWSGIServer)`:覆写 `process_request` 计数(逐行同构现有实现);`threaded=False`(fork-per-leaf 硬约束,m1 风险#5)
- [ ] `_handle_serve`:**fork 之前插入 `l2.restore()` 钩子**(L2 还原与 fork 原子化);quota/itimer 看门狗/per-leaf served-count pipe 语义全保留
- [ ] `_handle_freeze`:子进程 build 链完成后、发 frozen 前执行 `l2.snapshot_template()`
- [ ] `client.py: CheckpointHandle`(母本 `exp/snapshot_atropos.py: Checkpoint`):`serve(quota, budget)` + `freeze_child(name, build)` 契约不变——驱动端必须精确发送等量请求

### T2 L2 文件 DB 模板恢复(1.5d)`snapshot/l2_sqlite.py`(ADR-0004)
问题:fork COW 只隔离内存,叶对文件的写会落盘污染下一 epoch。
```python
class L2Sqlite:
    def snapshot_template(self):  # 冻结时:copy(live → tpl/<ckpt>/db.tpl);记录 size/sha
    def restore(self):            # 每叶 fork 前:copy(tpl → live);unlink live-journal/-wal/-shm
```
- **正确性论证**:检查点父进程的 SQLite 页缓存对应冻结时刻;模板=冻结时刻文件的位拷贝 → 还原后磁盘与父进程缓存**精确一致**(无脏页检测问题);被 SIGKILL 的叶可能留热 journal,删除即可(模板拷贝等价回滚)
- **约束**:目标 DB `journal_mode=DELETE`(adapter init 后断言 `PRAGMA journal_mode != 'wal'`);SQLAlchemy(VAmPI)每请求 teardown `db.session.remove()`,叶内无悬挂事务
- **粒度**:per-epoch 一次拷贝(引擎本就串行 waitpid);并行 epoch 列后续
- 验收数字:DB ≤1MB 时 `restore()` ≤2ms;带 L2 端到端恢复 ≥500 次/s(纯 fork 当前 1500-2000/s)

### T3 确定性重放执行器(1d)`executor/replay.py` + `observer`
- [ ] `run_segment(ckpt, seg, verify_steps, budget) -> EpochTrace`:ckpt.serve(quota) 后按 PlanStep 顺序发请求(WSGI 桥进程内直调或 HTTP,优先前者——Atropos"剔除 HTTP"教训);extract 观测值写回绑定上下文
- [ ] `extract.py`:JSON 点路径(`$.order_id`)优先,失败退正则;`session.py`:epoch 内 cookie/token 保持,epoch 间丢弃(状态由快照负责,不靠客户端)
- [ ] httpx.Client 每 epoch 复用连接

### T4 Atheris harness 双模式(2d)`fuzz/atheris_harness.py` + `fuzz/action_codec.py`
- [ ] **action_codec**:fuzz 字节 ↔ 结构化动作。编码: `[action_idx:u16][seg_len:u8][param 区:变长]`;action_idx 索引**动作词汇表**(SF3 前用 analyzer 端点表,SF3 后用规划链模板);多请求段(seg_len>1)表达链式输入(Atropos 多请求 input 思想)
- [ ] **in-process 模式(= B1 配置)**:harness 进程(ANOTA)内直接调 WSGI app(不 fork、不 HTTP)——覆盖反馈最大化、无隔离;oracle 违规(注解 RuntimeError)自然成为 libFuzzer crash
- [ ] **snapshot 模式(= B2 配置)**:harness 即检查点进程;`TestOneInput` fork 叶 → 叶内进程内调 WSGI → 观测/违规经 pipe 回报父进程 → 叶 `_exit`;epoch 由 quota 控制
- [ ] `-fork=1`(libFuzzer fork 模式)与 crash-artifact 去重回灌:多违规收集循环(SF0 已知默认首 crash 退出)
- [ ] 超时/预算:`-timeout` 与引擎 budget 双保险

### T5 跨 fork 覆盖反馈 spike(1.5-2d)⚠️ 攻坚
问题:snapshot 模式下叶的 atheris 计数页 COW 私有,父进程(harness)读不到 → 反馈丢失。
- [ ] **路线 A(优先)**:计数页共享——定位 atheris 2.0.12 的 `__sanitizer_cov_8bit_counters` 区段,父进程 `mmap(MAP_SHARED|MAP_ANONYMOUS)` 预留 + 子进程继承同一物理页(fork 天然共享 MAP_SHARED 页!):研究点=让 atheris 的计数缓冲落在共享映射上(`LD_PRELOAD` interpose 或 patch atheris native 模块);验证:叶跑新代码路径后父进程 counters 变化
- [ ] **路线 B(保底降级)**:每 epoch 反馈——叶退出前把 counters 哈希/差异经 pipe 发父进程,父进程合并入 libFuzzer(`__sanitizer_cov_trace_pc_guard` 不可直接喂,改为自定义 corpus 有效性判据);诚实记录与 Atropos(每输入反馈)的差距为 limitation
- [ ] 结论(无论 A/B)写入本文件 §5 与论文实验章节

## 3. 接口与数据流(核心签名)

```python
# fuzz/atheris_harness.py
def make_harness(spec: TargetSpec, mode: Literal["in_process","snapshot"],
                 vocab: ActionVocab, quota: int = 1, budget: float = 5.0) -> None
    # 在 ANOTA 进程内调用;atheris.instrument_all() 必须在 Setup 前

# snapshot/client.py
class CheckpointHandle:
    def serve(self, quota: int, budget: float | None) -> ServeMetrics
    def freeze_child(self, name: str, build: list[str]) -> "CheckpointHandle"

# executor/replay.py
class ReplayExecutor:
    def run_segment(self, ckpt, seg: PlanSegment, verify: list[Action]) -> EpochTrace
```

## 4. 测试

- **L2 零漂移**(硬验收):连续 20 epoch"建单 → 还原 → 单消失(SELECT 计数回模板值)";vulnshop 与 VAmPI(SQLAlchemy 文件 DB)各跑一遍
- epoch 隔离复演:vulnshop 上复刻 `exp/second_order.py` 三模式(baseline / per-input / epoch)行为一致
- 超时恢复后检查点健康(RegexDoS 类 payload,0.4-2s 看门狗)
- harness 双模式冒烟:vulnshop vuln 变体,in-process 60s 内命中 ≥1 注解违规(E17);snapshot 模式同(E18 前半)
- 恢复速率断言:带 L2 ≥500/s;纯 fork 不低于 exp/ 基线的 80%

## 5. 验收清单

- [ ] vulnshop:注册→登录→下单链从检查点 epoch 执行成功;恢复 ≤5ms;L2 零漂移 20/20
- [ ] VAmPI:同一正确性测试通过(SQLAlchemy 文件 DB)
- [ ] in-process 冒烟 E17 通过;snapshot 模式可检出且复跑一致
- [ ] **覆盖反馈 spike 出结论**(A 或 B),写入本节与 `plans/SF5-snapshot-policy.md` 的影响评估
- [ ] GUARDS 两变体在同一检查点树下可切换(重建根)

## 6. 风险与回退

| 风险 | 回退 |
|---|---|
| 路线 A 共享计数页做不成(atheris native 模块不可插) | 路线 B 每 epoch 反馈(SF5 消融会量化差距);这是能力差距不是正确性差距,诚实记录 |
| WSGI 进程内直调与 werkzeug 测试客户端行为差异 | 双实现开关(WSGI 桥 / 真 HTTP),golden 对比两者响应一致后默认 WSGI 桥 |
| SQLAlchemy 连接池跨 fork 异常 | adapter.init() 时 `poolclass=NullPool` 或 `create_engine(..., pool_size=0)`;叶内新建连接 |
| libFuzzer -fork=1 与我们的 fork 叶冲突 | harness 内不用 -fork=1:crash 后由驱动脚本重启 harness 并回灌 corpus(简单可靠) |
