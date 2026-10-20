# SS-07B 完成报告：可信 Session 证据、状态新颖度与基线绑定

## 状态

**已完成，准备独立提交。** 本报告只记录 SS-07B；SS-07C 的在线调度代码尚未
提交。

```text
Batch: SS-07B
Base commit: a249642
```

## 编码前基线

```text
git rev-parse HEAD
a249642  # SS-07A 完成
```

SS-07A 全量：`764 passed, 1 skipped, 26 deselected`（见 SS-07A 报告）。

## 改动

### 1. 证据带来源、观察点、客户端

`stateforge/state/session_observation.py`：

- `SessionSnapshotView` 新增 `trusted` / `observer_id`；`snapshot_view()`
  显式接收可信 observer ID。
- 新增 `SessionEvidence`：绑定
  `run_id / sequence_id / step_index / client_scope /
  observation_point_id / observer_id / observation_status /
  predicate_results / witness`。
- 新增 `build_session_evidence()`：
  - 只有 `trusted=True`、非 `legacy-*` 来源、非空 observer、合法
    `decoded/absent` 才可能生成 `witness`；
  - `UNKNOWN` / `invalid` / 空条件组 → `witness=None`，`trusted=False`；
  - 原始 dict 仍可作为单测兼容输入，但绝不能形成正式 `VERIFIED`。
- `session_gate` 目标注册
  `observer_id="flask-signed-cookie-reader"`，来源为
  `flask-signed-cookie`；模块探测注册 `module-session-observer`。

### 2. 状态新颖度归一化、run-scoped

新增 `stateforge/state/session_novelty.py`：

- `condition_signature()`：条件签名，不依赖目标名。
- `state_fingerprint()`：只保留稳定 key、粗粒度值类型
  （null/bool/number/string/container）和 decoded 状态；不保留原始
  Cookie、签名、nonce、随机 token。
- `SessionStateNovelty` 每个 run 独立维护：
  - `new_verified_session_fact`：条件组首次 `VERIFIED_TRUE`；
  - `new_verified_transition`：同一 client role 的 before/after 归一化
    状态变化；
  - `new_proxy_progress`：未达 verified 的独立代理进度；
  - `UNKNOWN` / `invalid` / 不可信观测不产生 verified 新颖度。
- 新颖度键包含 baseline、目标块、条件签名、client role、归一化值；
  `to_dict()` 不含原始 secret。

### 3. 检查点绑定 baseline

`stateforge/solving/session_replay.py`：

- `SessionReplayCheckpoint` 新增：
  `baseline_fingerprint / app_source_hash / session_backend /
  fixture_id / initialization_seed`。
- `baseline_fingerprint()` 对源码内容、配置、session backend、fixture、
  seed 做 SHA-256；不是只由目标名/路径推导。
- 正式准备检查点写入该 fingerprint；`replay_session_checkpoint()` 增加
  可选 `baseline=`，不匹配时在**任何 target 调用前**返回
  `UNKNOWN / baseline-mismatch`，不重放、不验证条件。
- 旧检查点没有 fingerprint 时保持可读（兼容旧 reader）；只有新检查点
  才执行强绑定。
- `find_verified_preparation_prefix()` 跳过不可信 view；dict probe 不能
  成为正式准备检查点。

## 反例测试

```text
stateforge/tests/state/test_session_evidence.py  6 passed
stateforge/tests/state/test_session_replay.py    18 passed
```

覆盖：

- raw dict / 空组 / 无 observer → 无 witness；
- 可信证据绑定 client 与 observation point；
- 随机 uid/nonce 变化不产生新 fact；
- invalid / unknown / untrusted 不产生 verified novelty；
- 跨 client 的 before/after 不产生 transition；
- novelty 字典不含原始 secret；
- baseline fingerprint 不匹配 → `UNKNOWN` 且不执行 target；
- baseline 匹配 → 准备前缀验证通过。

## 执行命令

```bash
python -m pytest stateforge/tests/state/ \
  stateforge/tests/evaluation/test_session_protocol.py \
  stateforge/tests/evaluation/test_session_ss07a.py -q

# 251 passed
```

## 已知限制

- 旧 v1 检查点没有 `baseline_fingerprint` 时走兼容路径；新协议生成的
  检查点才强制 baseline 绑定。
- 当前没有跨进程的 Session 后端数据库/文件状态建模；只建模目标暴露的
  只读 Session 视图。
- 新颖度只证明“首次出现的归一化 Session 事实”，不等价于目标成功；
  仍需独立 target witness 才算成功。
