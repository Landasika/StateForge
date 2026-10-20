# SS-07 总结：A–D 完成，E 预注册完成，F 未完成

## 最终门禁

```text
python -m pytest -q
783 passed, 1 skipped, 26 deselected, 1 warning in 637.34s
```

编码前基线：

```text
f415d963399d52c855475533d6e72b3ef16dc904
state/ fuzzing/ runtime/ = 317 passed
```

## 提交顺序

```text
a249642  SS-07A 真实 HTTP 计费/四臂公平口径
9058bb0  SS-07B 可信 Session 证据/归一化新颖度/baseline 绑定
d502c84  SS-07C Atheris 运行中消费 Session 反馈
9276070  SS-07D 受控回归/在线因果/冻结门禁
d363bf8  SS-07D 通过后写冻结清单
22be74d  SS-07E/F 五应用预注册与真实启动矩阵，拒绝伪造盲测
49c7f75  SS-07C 原始在线决策日志与协议冒烟产物
```

## 已验证

### SS-07A

- 真实 target-callable HTTP 计数，请求级/序列级原始 JSONL；
- D `bootstrap_http_requests=0`，无检查点记 `NO_CHECKPOINT`；
- wall-clock 单调测量，无硬编码 0；
- 3-seed 80 请求冒烟：root 741 = per_run 741。

### SS-07B

- `SessionEvidence` 绑定 client/observation point/observer；
- raw dict / invalid / unknown 不可能形成正式 witness；
- 随机 cookie/token 不产生新 Session fact；
- checkpoint 写入源码/配置/backend/fixture/seed 的 baseline fingerprint；
- mismatch 在任何 target 调用前 fail-closed。

### SS-07C

- Atheris 启动后由 handler 反馈驱动调度，custom mutator 消费下一焦点；
- 受控 10 输入：2 次 frontier switch、8 个 consumed guided mutation；
- 每个 decision 有原始 `scheduler_online.jsonl`。

### SS-07D

- 2 seeds × 4 臂 × 80 HTTP：
  - SINGLE 0/2；
  - SEQUENCE 1/2；
  - STATE_GUIDED 2/2；
  - STATE_REUSE 2/2，实际 7 HTTP，D bootstrap 0；
- 根日志 494 = per_run 494；
- 冻结门禁 9/9 通过后才写 `freeze_manifest.json`。

## 未完成

SS-07F 的五个真实应用盲测 **没有执行**：

- 预注册：5/5，源码树 SHA 已保存；
- 启动探针：4/5 `SMOKE_PASS`，Spirit `ENV_BLOCKED`
  （缺 `test_project`）；
- 实际 arm 运行：**0**；
- `FREEZE_REFUSED.json` 明确记录零 arm，未写成功冻结清单。

原因不是“应用启动失败”，而是缺少每个真实应用的 Session target
adapter（真实 observer、HTTP target、隔离账号/DB、request pool、target
block）。health 200 不能冒充 `target_reached`，受控 `session_gate` 的
2/2 也不能冒充五应用结果。

## 结论

A–D 可在受控目标上复现并通过全量测试；E 的预注册和启动证据已保存；
F 仍被真实应用适配缺口阻塞，不能声称 SS-07F 完成。
