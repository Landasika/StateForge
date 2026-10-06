# Patch 0002: ANOTA_STATE — 第五类 annotation

## 变更文件(ANOTA 解释器,不在此仓库)

| 文件 | 操作 |
|------|------|
| `Include/anota_state.h` | NEW |
| `Python/anota_state.c` | NEW (~350 行) |
| `Python/bltinmodule.c` | +`#include "anota_state.h"` + `_PyAnotaState_GetSingleton()` 注册 |
| `Makefile.pre.in` | +`Python/anota_state.o`(3 处) |
| `Makefile` | +`Python/anota_state.o`(3 处) |
| `anota_state_test.py` | NEW(16 tests) |

## API

```python
ANOTA_STATE.DEFINE(obj, key, name=None, domain=None, values=None)
ANOTA_STATE.WRITE(obj, key, value, writer=None)
ANOTA_STATE.REQUIRE(obj, key, op='EQ', value=None)  # 不 crash,返回 dict
ANOTA_STATE.TRANSITION(obj, key, from_=None, to=None, action=None)
ANOTA_STATE.GET(obj, key) → dict | None
ANOTA_STATE.CLEAR(obj, key)
ANOTA_STATE.CLEAR_ALL()
```

## 与 ANOTA_WATCH 的关键差异

| | WATCH | STATE |
|---|---|---|
| 身份 | `id(obj), key` | 语义 `domain:type:entity:field` |
| 失败行为 | RuntimeError | 结构化 dict(`STATE_REQUIREMENT_UNSATISFIED`) |
| 侵入 ceval.c | 是(LOAD/STORE 拦截) | **否**(纯显式 annotation) |
| 目的 | sanitizer(crash=fuzz signal) | state-reachability feedback |

## REQUIRE 返回格式

```python
{
    "state_id": "APP:Order:status",
    "status": "SATISFIED" | "STATE_REQUIREMENT_UNSATISFIED" | "UNKNOWN_OP",
    "required_op": "EQ",
    "required_value": "PAID",
    "observed_value": "CREATED",
}
```

## 重建方法

```bash
cd ~/icassp/anota/ANOTA/ANOTA-interpreter
gcc -c -I. -IInclude -o Python/anota_state.o Python/anota_state.c -fPIC -g -O0
make -j$(nproc)
./python anota_state_test.py -v
```

## 回归

- 16/16 STATE tests PASS
- 原 WATCH/EXECUTION tests PASS
- TAINT SystemError = artifact 既有 bug(非本次引入)
- StateForge 244 tests 全绿
