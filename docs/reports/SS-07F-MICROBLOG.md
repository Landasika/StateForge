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

## linkding（追加）

```text
adapter: stateforge/targets/real_apps/linkding/inproc_target.py
target: views/bookmarks.py::index::43-43::entry::1
condition: Session._auth_user_id EXISTS
```

```text
C bootstrap  found=true, VERIFIED, 2 steps, 6 executions
D reuse      prefix_len=1, PREPARATION_VERIFIED,
             primary_suffix_target=true, isolation=true,
             target_http_requests=6, bootstrap_http_requests=0
```

## healthchecks（追加）

```text
adapter: stateforge/targets/real_apps/healthchecks/inproc_target.py
target: accounts/views.py::profile::288-288::entry::1
condition: Session._auth_user_id EXISTS
```

```text
C bootstrap  found=true, VERIFIED, 2 steps, 6 executions
D reuse      prefix_len=1, PREPARATION_VERIFIED,
             primary_suffix_target=true, isolation=true,
             target_http_requests=6, bootstrap_http_requests=0
```

## 当前覆盖

```text
microblog    C/D + 单 seed 四臂
CTFd         C/D
healthchecks C/D
linkding     C/D
Spirit       test_project 已生成, 适配器待接
```

仍不是五应用完整盲测；没有写 `frozen=true`。

## Spirit（追加，环境阻塞已解除）

之前 `ENV_BLOCKED` 的原因是仓库里没有 `test_project`。用 Spirit 自己的
CLI 生成了测试项目：

```bash
cd external/Spirit
PATH=$PWD/.venv/bin:$PATH .venv/bin/spirit startproject test_project
```

然后接入：

```text
adapter: stateforge/targets/real_apps/spirit/inproc_target.py
target: user/views.py::update::24-24::entry::1
condition: Session._auth_user_id EXISTS
```

```text
C bootstrap  found=true, VERIFIED, 2 steps, 6 executions
D reuse      prefix_len=1, PREPARATION_VERIFIED,
             primary_suffix_target=true, isolation=true,
             target_http_requests=6, bootstrap_http_requests=0
```

## 五应用当前覆盖

```text
microblog    C/D + 单 seed 四臂
CTFd         C/D
healthchecks C/D
linkding     C/D
Spirit       C/D（test_project 已生成）
```

**仍然不是完整五应用盲测**：没有做五应用 × 多 seed × 四臂，也没有冻结。
`blind_fuzzing_claimed=false`、`frozen=false` 保持不变。

## 五应用 C/D 与单 seed SEQUENCE

```text
app           C/STATE_GUIDED   D/STATE_REUSE   SEQUENCE
microblog     PASS             PASS            PASS
CTFd          PASS             PASS            FAIL (40)
healthchecks  PASS             PASS            FAIL (40)
linkding      PASS             PASS            PASS (29)
Spirit        PASS             PASS            FAIL (40)
```

C/D 五应用全部真实跑通；SEQUENCE 在三个应用上 40 次内未找到。
这仍然是 **单 seed 的功能对照**，不能当作统计结论。

## 单 seed × 五应用 × 四臂

已补齐 SINGLE 的 10-input smoke，并生成机器可读矩阵：

```text
artifacts/session_ss07f/MATRIX_SEED1.json
```

摘要：

```text
app           SINGLE  SEQUENCE  STATE_GUIDED  STATE_REUSE
microblog     false   true      true          true
CTFd          false   false     true          true
healthchecks  false   false     true          true
linkding      false   true      true          true
Spirit        false   false     true          true
```

注意：SINGLE 是 10 个输入的 smoke，不是与搜索臂等价的 80 预算；
SEQUENCE 在三应用 40 次搜索内失败。因此这仍不是公平的多 seed 盲测，
`blind_fuzzing_claimed=false`、`frozen=false` 不变。

## 独立进程 replay

新增 `stateforge/evaluation/real_app_replay.py`：在**新的解释器/应用进程**
中加载已保存 checkpoint，重新打开真实应用，重放 1 步前缀 + 1 步 suffix，
不读取实验进程状态。

五应用全部通过：

```text
app           status                 prefix  suffix_target
microblog     PREPARATION_VERIFIED   1       true
CTFd          PREPARATION_VERIFIED   1       true
healthchecks  PREPARATION_VERIFIED   1       true
linkding      PREPARATION_VERIFIED   1       true
Spirit        PREPARATION_VERIFIED   1       true
```

每次 replay 成本：

```text
target_http_requests = 3
checkpoint_validation = 1
prefix_replay = 1
suffix = 1
bootstrap_http_requests = 0
```

原始结果在：

```text
artifacts/session_ss07f/independent_replay/<app>/replay.json
```

healthchecks 第一次独立 replay 因登录 token bucket 被前面的实验耗尽而失败，
清除该应用自身的限流桶后重跑通过；失败记录没有隐藏。

## seed 2 的 C/D

新增 `stateforge/evaluation/real_app_point.py`，按 (app, seed) 记录 C/D。

```text
app           C found   D suffix target   D status
microblog     true      true              PREPARATION_VERIFIED
CTFd          true      true              PREPARATION_VERIFIED
healthchecks  true      true              PREPARATION_VERIFIED
linkding      true      true              PREPARATION_VERIFIED
Spirit        true      false              REPLAY_FAILED
```

Spirit seed 2 的 D 失败发生在同一进程里 C 先登录、D 再登录时，被应用自身
的登录限流挡住；没有隐藏这个失败，也没有把它写成通过。

多 seed 结果：

```text
artifacts/session_ss07f/MATRIX_SEED2.json
```

## seed 3 与 1–3 汇总

seed 3 的 C 五应用全部通过，D 在同进程内 4/5 通过，Spirit 再次被同进程
登录限流挡住；随后用**新进程** replay Spirit 的 checkpoint，通过。

```text
C bootstrap:  seed 1/2/3 × 5 = 15/15
D 同进程:     seed 1 5/5; seed 2/3 各 4/5（Spirit 失败）
D 新进程:     Spirit seed 2/3 replay 通过
```

汇总文件：

```text
artifacts/session_ss07f/MATRIX_SEEDS_1_3.json
```

这证明 C 在三个 seed 上稳定；D 的 Spirit 同进程失败是协议层面的登录预算问题，
不是 checkpoint 无效。仍然没有完整四臂 × 多 seed，也没有冻结。

## 修复 Spirit 同进程 D 限流

给 `real_app_point.py` 加了 `--d-only`：复用已保存的 C 结果，在**新进程**
中只跑 D。Spirit seed 2/3 用该模式重跑：

```text
seed 2  D  PREPARATION_VERIFIED  suffix_target=true  isolation=true
seed 3  D  PREPARATION_VERIFIED  suffix_target=true  isolation=true
```

因此现在：

```text
C/D 15/15
```

其中 seed 2/3 的 Spirit 是 D-only 新进程；同进程失败仍保留在历史记录里。
这不是完整四臂多 seed，`frozen=false` 不变。

## SEQUENCE 多 seed

新增 `stateforge/evaluation/real_app_sequence.py`，并按 seed 2/3 跑五应用：

```text
app           seed1  seed2  seed3
microblog     true   true   true
CTFd          false  false  false
healthchecks  false  true   false
linkding      true   true   true
Spirit        false  false  false
```

SEQUENCE 是 40 次结构搜索，不是 Atheris 输入预算。汇总：

```text
artifacts/session_ss07f/MATRIX_ARMS_SEEDS.json
```

结果说明 CTFd/Spirit 在这个请求池上结构搜索找不到登录序列，但
STATE_GUIDED 可以；这正是 Session 引导的差异，不能把 SEQUENCE 失败抹掉。
