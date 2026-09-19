# ANOTA 注解修改 0001:WATCH 违规消息可归因化(2026-09-19)

## 动机

v2.1 第一步("ANOTA 注解修改")的基石:`ANOTA_WATCH.BLOCK(obj, modes, key)`
的违规原本只抛 `RuntimeError("ANOTA_WATCH policy violation")`(细节仅进 stderr),
HTTP 信道无法归因。补丁让异常消息携带 kind/mode/key,与 `ANOTA_EXECUTION.BLOCK`
的 `msg=` 同等可归因——**状态读写的运行时观测事件**从此可经 500 响应体回收
(正则 `ANOTA_WATCH_POLICY:(\w+):?('?\w*'?)`),供 G_xfg 运行时校准。

## 修改位置:`anota/ANOTA/ANOTA-interpreter/Python/anota_watch.c`

`_anota_check_access()` 内两处 `_PyErr_SetString` → `_PyErr_Format`:

```c
/* 第一处:键级策略(约 263 行起) */
- _PyErr_SetString(tstate, PyExc_RuntimeError, "ANOTA_WATCH policy violation");
+ _PyErr_Format(tstate, PyExc_RuntimeError,
+               "ANOTA_WATCH_POLICY:%s_%s:%R", kind_str, mode_str, key);

/* 第二处:对象级策略(约 280 行起) */
- _PyErr_SetString(tstate, PyExc_RuntimeError, "ANOTA_WATCH policy violation");
+ _PyErr_Format(tstate, PyExc_RuntimeError,
+               "ANOTA_WATCH_POLICY:%s_%s", kind_str, mode_str);
```

## 重建(本机已执行,二进制备份 `python.bak-20260919`)

```bash
cd anota/ANOTA/ANOTA-interpreter && make -j8 python
```

## 验证(已通过)

```
W.BLOCK(r, 'W', key='status') 后 r.status = 'PAID'
  → RuntimeError: ANOTA_WATCH_POLICY:member_W:'status'
W.BLOCK(r2, 'R') 后读属性
  → RuntimeError: ANOTA_WATCH_POLICY:object_R
回归:vulnshop 靶场+信道 15/15、快照栈 15/15(重建无副作用)
```

## 同场发现的既有 bug(绕过不修,与已知 TAINT C 层 bug 同族)

对携带污点的对象做下标切片(如 `str(e)[:80]`)会令策略查找收到 slice 键
→ `TypeError: unhashable type: 'slice'`。写注解代码时避免在污点值上切片。
