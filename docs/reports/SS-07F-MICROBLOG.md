# SS-07F 进展：microblog 真实应用首次跑通

## 状态

**microblog 已接入真实应用适配器，C 的 bootstrap 与 D 的复用已跑通。**
其余四个应用仍只有启动探针；SS-07F 还未完成。

## 适配器

新增：

```text
stateforge/targets/real_apps/microblog/fuzz_target.py   # 真实 HTTP 客户端
stateforge/targets/real_apps/microblog/inproc_target.py # 进程内 test_client
artifacts/session_ss07f/microblog/contract.json          # 预注册 Session 合约
artifacts/session_ss07f/microblog/request_pool.json
```

`inproc_target.py` 运行 microblog 自身 Flask app，使用真实 test client、
真实 Cookie Jar，并用 app 自身的 signed-cookie serializer 做只读观察。
`contract.json` 是应用级预注册元数据：

```text
target: main/routes.py::index::29-29::block::1
condition: Session._user_id EXISTS
writer: auth/routes.py::login::25-26::block::1
```

这不是引擎内目标规则；引擎仍只消费通用 `SessionFrontier`/
`SessionCondition`。

## C：真实应用 bootstrap

输入：

```text
POST /auth/login  {"username":"fuzz","password":"fuzzpass"}
GET  /index
```

结果：

```text
found = true
status = VERIFIED
steps = 2
executions = 7
```

即：真实 microblog 应用上，静态合约 + 真实登录请求 + 真实 Session
观察，找到了 `login → index` 的已验证序列。原始产物：

```text
artifacts/session_ss07f/microblog/c_bootstrap/session_guided/result.json
artifacts/session_ss07f/microblog/c_bootstrap/per_request.jsonl
```

## D：真实应用前缀复用

从 C 的已验证序列提取 1 步准备前缀（登录），独立验证后，冷启动重放：

```text
checkpoint prefix_len = 1
checkpoint_kind = state_preparation
validation = PREPARATION_VERIFIED
primary suffix GET /index  -> suffix_target_covered = true
isolation control          -> true
```

成本：

```text
target_http_requests = 9
checkpoint_validation = 4
prefix_replay = 2
suffix = 3
bootstrap_http_requests = 0
elapsed_wall_seconds = 0.793279
```

即 D 没有重新 bootstrap，只重放已验证登录前缀和 `/index` 后缀。
原始产物：

```text
artifacts/session_ss07f/microblog/d_reuse/result.json
artifacts/session_ss07f/microblog/d_reuse/per_request.jsonl
artifacts/session_ss07f/microblog/d_reuse/sc-a4cf478e34e0.json
```

## microblog 四臂（1 seed）

```text
SINGLE        target=false, HTTP=10, Atheris inputs=10
SEQUENCE      target=true,  VERIFIED, 40 search executions
STATE_GUIDED  target=true,  VERIFIED, 7 search executions
STATE_REUSE   target=true,  HTTP=9, bootstrap=0, prefix_len=1, isolation=true
```

这是单应用、单 seed 的功能对照，不是五应用统计结论。

## 仍然缺什么

- 只跑了 microblog；没有跑五应用 × 多 seed 的完整盲测。
- `/explore` 作为第二后缀未命中目标 block（`suffix_target_covered=false`），
  未把它写成成功。
- CTFd、healthchecks、linkding 只有启动探针；Spirit 仍缺 `test_project`。
- 因此 `blind_fuzzing_claimed=false`，不写五应用成功冻结清单。

原始启动矩阵仍在：

```text
artifacts/session_ss07ef/SS07EF_SUMMARY.json
```

本次 microblog 结果见：

```text
artifacts/session_ss07f/SS07F_SUMMARY.json
```

## CTFd（追加）

CTFd 也接入了同样的进程内真实 Flask test client：

```text
adapter: stateforge/targets/real_apps/ctfd/inproc_target.py
contract: artifacts/session_ss07f/ctfd/contract.json
target: challenges.py::listing::24-24::entry::1
condition: Session.id EXISTS
writer: auth.py::login::483-488::block::1
```

结果：

```text
C bootstrap  found=true, VERIFIED, 2 steps, 6 executions
D reuse      prefix_len=1, PREPARATION_VERIFIED,
             primary_suffix_target=true, isolation=true,
             target_http_requests=6, bootstrap_http_requests=0
```

CTFd 的 Session 是服务端存储，所以观察器不假装解码 cookie，而是在
`test_request_context` 中通过应用自身 Session 接口读取 `id`。
