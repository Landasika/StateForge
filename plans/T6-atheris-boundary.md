# T+6 Atheris 边界修复 —— 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 恢复 Atheris 覆盖引导闭环(选择性插桩 + Flask 完整分发),接入 StateForge 结构 trace(单调 request_id + JSONL 增量落盘),达到与旧 HEAD harness 的量化对齐。

**Architecture:** 两层边界——`atheris_runner` 管 Atheris 生命周期 + 共享 TraceSession + JSONL writer;`adapters/` 管目标加载/插桩/fuzz bytes→请求。Atheris coverage 由 `instrument_func` 字节码插桩负责,StateForge trace 由 `TraceSession` 负责,两层独立共存(判别矩阵 A死/B活/C活 已证)。

**Tech Stack:** Python 3.10(ANOTA)/3.13(系统)双兼容、Atheris(libFuzzer)、Flisk test_request_context、pytest。

**Spec:** 本计划实现 Codex 十点方案 + 会话中确认的 5 处补强(tracer 热路径、trace 保留策略、corpus 目录传入、JSONL loader、量化验收)。上游设计:`plans/design-v2-state-reachability.md` §8 T+6(in-process=B1 模式)。

## Global Constraints

- 代码必须同时在系统 CPython 3.13 与 ANOTA CPython 3.10 下运行:禁止 3.11+ 语法(不用 match-case、不用 `Self`、ExceptionGroup)。
- `atheris` 只能懒导入(函数体内 import);单测不得依赖 atheris 存在(`pytest.importorskip` 守护)。
- 禁止 `atheris.instrument_all()` 作为默认路径(旧 harness 实证:与 ANOTA 内部模块冲突)。
- 输入回调内任何异常不得逃逸到 libFuzzer。
- trace 产物持久化不得依赖进程正常退出(finally/atexit 已实证被 libFuzzer `_Exit` 绕过)。
- 每个任务结束:全测试套件 `python -m pytest -q stateforge/tests/` 绿 + commit。
- commit 消息末尾加 `Co-Authored-By: Claude Code <noreply@anthropic.com>`。

## 判别实验存档(本计划的事实依据)

| 配置 | 结果 |
|---|---|
| 无插桩、无 tracer、直调 target | ❌ no interesting inputs |
| `instrument_func(views)`、无 tracer | ✅ cov→32 |
| `instrument_func(views)` + `run_one` tracer(sys.settrace 活跃) | ✅ cov→45 |
| 旧 HEAD harness(ANOTA,插桩,无 tracer) | ✅ cov→11(12s),历史 3733 exec/s |
| `run_atheris` 现状(无插桩) | ❌ 两解释器均死 + trace 丢失 |
| 拼接 trace request_id 重置 | ❌ 伪造边 login:153→read_order:218 |

---

### Task 0: 基线固化(当前 ~1300 行未提交重构)

**Files:**
- Modify: git index only(工作树内容不动)

**Interfaces:**
- Produces: 一个干净的基线 commit,后续任务各自成 commit 可回滚。

- [x] **Step 1: 检查工作树状态,确认无意外文件**

Run: `git status --short`
Expected: 与会话开头快照一致(8 个修改 + 新模块目录 + `1.tar.gz`)。`1.tar.gz` **不加入** commit。

- [x] **Step 2: 提交基线(不含 1.tar.gz)**

```bash
git add stateforge/README.md stateforge/core/ stateforge/dynamic/ stateforge/feedback/ \
        stateforge/fuzz/ stateforge/static/ stateforge/tests/structural/ \
        stateforge/cli.py stateforge/analyzer/pycg_bridge.py \
        stateforge/targets/vulnshop/.coverage_lines.json
git rm --cached -q stateforge/fuzz/atheris_harness.py stateforge/fuzz/coverage_viz.py \
        stateforge/fuzz/gap_analysis.py stateforge/fuzz/guided_explorer.py 2>/dev/null || \
git add -u stateforge/fuzz/
git commit -m "第一层结构反馈管道:core/static/dynamic/feedback 重构 + CLI 三命令(analyze/feedback/experiment)

ICFG/trace/overlay/frontier/distance/scoring 全链路;旧 fuzz 四件套(atheris_harness/coverage_viz/gap_analysis/guided_explorer)删除。已知缺口:run_atheris 边界(插桩/落盘/request_id)由 T+6 修复。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

- [x] **Step 3: 验证基线测试绿**

Run: `python -m pytest -q stateforge/tests/`
Expected: 57 passed, 1 skipped(与本会话实测一致)

---

### Task 1: tracer 热路径优化(缺口 1,先做——后续所有 exec/s 验收依赖它)

**Files:**
- Modify: `stateforge/dynamic/tracer.py`(全文约 88 行,改 `_trace` 与 `__init__`)
- Test: `stateforge/tests/structural/test_tracer_perf.py`

**Interfaces:**
- Consumes: 现有 `TraceSession`/`TraceEvent`(签名不变)
- Produces: `TraceSession` 行为完全不变;新增内部 `_file_cache: dict[str, tuple[bool, str]]`(filename → (在 root 内?, 已解析绝对路径))。事件里的 `file` 字段仍存**已解析**路径(与现在一致)。

- [x] **Step 1: 写失败测试(过滤缓存正确性:root 内记录已解析路径、root 外不记录、同文件第二次事件走缓存仍正确)**

```python
"""Tracer file-filter cache: correctness of the hot path."""
from pathlib import Path

from stateforge.dynamic.tracer import TraceSession


def test_events_use_resolved_paths_and_respect_root(tmp_path: Path):
    source = tmp_path / "app.py"
    source.write_text(
        "def outer():\n"
        "    inner()\n"
        "    inner()\n"   # 第二次调用:同文件事件走缓存路径
        "def inner():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    ns: dict = {}
    exec(compile(source.read_text(), str(source), "exec"), ns)
    session = TraceSession(tmp_path)
    with session:
        ns["outer"]()
    assert session.events, "root 内文件必须有事件"
    assert all(str(e.file).startswith(str(tmp_path)) for e in session.events)
    # 同文件多事件(缓存命中路径)也要映射成功
    assert sum(1 for e in session.events if e.function == "inner") >= 4
    # root 外不记录:trace 期间调用标准库
    session2 = TraceSession(tmp_path)
    with session2:
        ns["outer"]()
        import json as _json
        _json.dumps({"a": 1})  # 标准库文件,不在 root
    assert not any("json" in e.file for e in session2.events)


def test_no_root_traces_everything(tmp_path: Path):
    source = tmp_path / "app.py"
    source.write_text("def f():\n    return 1\n", encoding="utf-8")
    ns: dict = {}
    exec(compile(source.read_text(), str(source), "exec"), ns)
    session = TraceSession(None)
    with session:
        ns["f"]()
    assert any(str(e.file).startswith(str(tmp_path)) for e in session.events)
```

- [x] **Step 2: 跑测试确认现状**

Run: `python -m pytest -q stateforge/tests/structural/test_tracer_perf.py`
Expected: PASS(现有实现语义已满足)——本任务是**性能重构,行为保持**;测试先行的目的是锁死行为再动实现。若 FAIL,先弄清再继续。

- [x] **Step 3: 重构 `_trace` 为缓存过滤(每事件只做 dict 查找 + 前缀判断)**

`tracer.py` 顶部加 `import os`;`__init__` 末尾加 `self._file_cache: dict[str, tuple[bool, str]] = {}`;`_trace` 整体替换为:

```python
    def _trace(self, frame, event, arg):
        if event not in {"call", "line", "return"}:
            return self._trace
        filename = frame.f_code.co_filename
        cached = self._file_cache.get(filename)
        if cached is None:
            resolved = str(Path(filename).resolve())
            if self.root is None:
                cached = (True, resolved)
            else:
                inside = resolved == self.root or resolved.startswith(self.root + os.sep)
                cached = (inside, resolved)
            self._file_cache[filename] = cached
        inside, resolved = cached
        if inside:
            rid = getattr(self, "_active_request", 0)
            with self._lock:
                index = self._request_indices.get(rid, 0)
                self._request_indices[rid] = index + 1
            self.events.append(TraceEvent(rid, index, resolved,
                                          frame.f_code.co_name, frame.f_lineno,
                                          event))
        return self._trace
```

(语义与原版一致:root 判断用 `is_relative_to` 的等价展开,含相等;事件存 resolved 路径。`Path.is_relative_to` 在 3.10 存在,但字符串比较更快且等价。)

- [x] **Step 4: 跑全套确认绿 + 微基准记录**

Run: `python -m pytest -q stateforge/tests/` → 57 passed, 1 skipped(+新增 2)
Run: `python - <<'EOF'
import time, sys
sys.path.insert(0, "/home/landasika/icassp")
from stateforge.dynamic.tracer import TraceSession
def burn(n):
    for i in range(n):
        abs(i * 7 % 13)
session = TraceSession("/home/landasika/icassp/stateforge")
t0 = time.perf_counter(); n0 = 0
with session:
    for _ in range(2000):
        burn(20); n0 = len(session.events)
dt = time.perf_counter() - t0
print(f"tracer throughput: {n0/dt:.0f} events/s")
EOF`
Expected: 记录数字到提交信息(优化前同脚本 ~几千 events/s 量级,优化后应显著上升;只记录,不设硬门槛——硬门槛在 Task 7 的 exec/s gate)。

- [x] **Step 5: Commit**

```bash
git add stateforge/dynamic/tracer.py stateforge/tests/structural/test_tracer_perf.py
git commit -m "T6-1:tracer 热路径优化——文件过滤缓存,免除逐事件 Path.resolve(每事件一次文件系统调用)

行为不变(测试锁定):root 内记已解析路径、root 外不记、同文件缓存命中。基准:<数字> events/s。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 2: 选择性插桩组件 instrumentation.py

**Files:**
- Create: `stateforge/fuzz/instrumentation.py`
- Test: `stateforge/tests/structural/test_instrumentation.py`

**Interfaces:**
- Produces:
  - `class Instrumenter`:`instrument_callable(func, atheris) -> bool`(新插桩返回 True,已插桩/不可插桩/失败返回 False);`instrument_flask_app(app, atheris) -> int`(视图+闭包内部函数,去重,返回新插桩数)
  - 实例级去重(`Instrumenter()` 自带 registry,无全局状态);去重键 = `(co_filename, co_firstlineno, co_name)`,插桩成功后**同时标记原 code key 与插桩后 code key**(防二次插桩)。

- [x] **Step 1: 写失败测试**

```python
"""Selective instrumentation with dedup."""
from flask import Flask

from stateforge.fuzz.instrumentation import Instrumenter


class FakeAtheris:
    def __init__(self):
        self.calls: list = []

    def instrument_func(self, func):
        self.calls.append(func)   # 真实 atheris 会改写 __code__;假件只记录


def _app_with_closure() -> Flask:
    app = Flask(__name__)
    factor = 2

    @app.get("/double/<int:n>")
    def double(n: int):
        def scale(v):          # 闭包内部函数,必须被插桩
            return v * factor
        return {"r": scale(n)}

    @app.post("/boom")
    def boom():
        raise RuntimeError("boom")

    return app


def test_instruments_views_and_closure_cells_once():
    app = _app_with_closure()
    atheris = FakeAtheris()
    inst = Instrumenter()
    first = inst.instrument_flask_app(app, atheris)
    calls_after_first = len(atheris.calls)
    second = inst.instrument_flask_app(app, atheris)
    assert first >= 2          # double + scale(闭包)+ boom
    assert second == 0         # 全部去重
    assert len(atheris.calls) == calls_after_first


def test_double_instrument_of_same_function_is_rejected():
    atheris = FakeAtheris()

    def f(x):
        return x + 1

    assert Instrumenter().instrument_callable(f, atheris) is True
    assert Instrumenter().instrument_callable(f, atheris) is False  # 新实例也拒(同 code key)
    assert len(atheris.calls) == 1


def test_non_callable_and_instrument_failure_are_safe():
    atheris = FakeAtheris()

    class Exploding:
        def instrument_func(self, func):
            raise ValueError("not instrumentable")

    inst = Instrumenter()
    assert inst.instrument_callable(None, atheris) is False
    assert inst.instrument_callable(42, atheris) is False

    def g():
        return 1

    assert inst.instrument_callable(g, Exploding()) is False   # 失败不上 registry
    assert inst.instrument_callable(g, atheris) is True        # 失败后可重试
```

注意 `test_double_instrument_of_same_function_is_rejected` 用**两个 Instrumenter 实例**验证同一函数不重复插桩——这意味着 registry 必须是**模块级共享**的。修正接口设计:`Instrumenter(registry=None)`,默认使用模块级默认 registry;测试里显式传 `Instrumenter(registry=set())` 获得隔离。两个测试改用:

```python
    assert Instrumenter(registry=set()).instrument_callable(f, atheris) is True
    assert Instrumenter(registry=set()).instrument_callable(f, atheris) is False
```

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest -q stateforge/tests/structural/test_instrumentation.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'stateforge.fuzz.instrumentation'`

- [x] **Step 3: 实现**

```python
"""Selective Atheris bytecode instrumentation.

Atheris coverage comes from ``instrument_func`` rewriting a function's
bytecode to call ``_trace_branch``.  Rewriting the already-rewritten code
double-instruments, so dedup keys (file, firstlineno, name) of both the
original and the instrumented code object are recorded in a registry.
``instrument_all`` is intentionally not offered: instrumenting ANOTA's own
internals conflicts with the interpreter (see old atheris_harness.py notes).
"""

from __future__ import annotations

from types import CodeType

_DEFAULT_REGISTRY: set[tuple[str, int, str]] = set()


def _code_key(code: CodeType) -> tuple[str, int, str]:
    return (code.co_filename, code.co_firstlineno, code.co_name)


class Instrumenter:
    def __init__(self, registry: set[tuple[str, int, str]] | None = None):
        self._seen = _DEFAULT_REGISTRY if registry is None else registry

    def instrument_callable(self, func, atheris) -> bool:
        code = getattr(func, "__code__", None)
        if code is None or not callable(func):
            return False
        if _code_key(code) in self._seen:
            return False
        try:
            atheris.instrument_func(func)
        except Exception:
            return False          # 失败不登记,可重试
        self._seen.add(_code_key(code))
        rewritten = getattr(func, "__code__", None)
        if rewritten is not None:
            self._seen.add(_code_key(rewritten))
        return True

    def instrument_flask_app(self, app, atheris) -> int:
        count = 0
        for rule in app.url_map.iter_rules():
            if rule.endpoint == "static":
                continue
            view = app.view_functions.get(rule.endpoint)
            if self.instrument_callable(view, atheris):
                count += 1
            for cell in getattr(view, "__closure__", None) or ():
                try:
                    inner = cell.cell_contents
                except ValueError:      # 空 cell
                    continue
                if self.instrument_callable(inner, atheris):
                    count += 1
        return count
```

(FakeAtheris 不改写 `__code__`,所以假件路径下 rewritten key == 原 key,幂等仍成立。)

- [x] **Step 4: 跑测试确认通过 + 全套绿**

Run: `python -m pytest -q stateforge/tests/structural/test_instrumentation.py` → 4 passed
Run: `python -m pytest -q stateforge/tests/` → 全绿

- [x] **Step 5: Commit**

```bash
git add stateforge/fuzz/instrumentation.py stateforge/tests/structural/test_instrumentation.py
git commit -m "T6-2:选择性 Atheris 插桩组件——视图+闭包、code-key 去重(防二次改写字节码)、失败可重试

不提供 instrument_all(旧 harness 实证与 ANOTA 内部模块冲突)。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 3: 目标适配层 adapters/(base + callable + flask)

**Files:**
- Create: `stateforge/fuzz/adapters/__init__.py`(re-export 三个类)
- Create: `stateforge/fuzz/adapters/base.py`
- Create: `stateforge/fuzz/adapters/callable.py`
- Create: `stateforge/fuzz/adapters/flask.py`
- Test: `stateforge/tests/structural/test_adapters.py`

**Interfaces:**
- Consumes: `Instrumenter`(Task 2)
- Produces:
  - `TargetAdapter`(Protocol):`prepare(self, atheris) -> None`(Fuzz 前一次性:插桩等)、`execute(self, data: bytes) -> None`(单次目标执行,异常允许抛出——由 runner 捕获记录)
  - `CallableAdapter(target, instrument=True)`(普通 `target(bytes)` 函数)
  - `RouteSpec(url, endpoint, method, json_body=None)` frozen dataclass
  - `FlaskAdapter(app, headers=None, bodies=None)`:`.routes: list[RouteSpec]`;`bodies` 值为 dict 或 `Callable[[bytes], dict]`;`execute` 经 `test_request_context` + `full_dispatch_request()` 返回 `Response | None`(供测试观察,runner 忽略返回值)

- [x] **Step 1: 写失败测试**

```python
"""Target adapters: fuzz bytes -> one target execution."""
import pytest
from flask import Flask

from stateforge.fuzz.adapters.base import TargetAdapter
from stateforge.fuzz.adapters.callable import CallableAdapter
from stateforge.fuzz.adapters.flask import FlaskAdapter


class FakeAtheris:
    def __init__(self):
        self.calls = []

    def instrument_func(self, func):
        self.calls.append(func)


def _app() -> Flask:
    app = Flask(__name__)

    @app.get("/items/<int:n>")
    def item(n: int):
        return {"n": n}

    @app.post("/orders")
    def create():
        from flask import request
        body = request.get_json(silent=True) or {}
        return {"total": body.get("total", 0)}, 201

    @app.get("/raise")
    def raiser():
        raise RuntimeError("view error")

    return app


def test_protocol_conformance():
    adapter = FlaskAdapter(_app())
    assert isinstance(adapter, TargetAdapter)


def test_routes_enumerated_path_params_prefilled():
    adapter = FlaskAdapter(_app())
    urls = {r.endpoint: r.url for r in adapter.routes}
    assert urls["item"] == "/items/1"          # 路径参数预填
    create = next(r for r in adapter.routes if r.endpoint == "create")
    assert create.method == "POST"             # 非_GET_路由取真实方法
    assert len(adapter.routes) == 3


def test_execute_dispatches_full_request_with_body():
    adapter = FlaskAdapter(_app(), bodies={"create": lambda d: {"total": d[1]}})
    resp = adapter.execute(b"\x01\x07")        # 第 2 条路由(create)
    assert resp is not None and resp.status_code == 201
    assert b'"total": 7' in resp.data          # bytes 参与了 body 构造


def test_execute_swallows_nothing_view_exception_propagates():
    adapter = FlaskAdapter(_app())
    with pytest.raises(RuntimeError):
        adapter.execute(b"\x02")               # raiser —— 异常边界在 runner


def test_callable_adapter_prepare_instruments():
    atheris = FakeAtheris()
    calls = []

    def target(data: bytes):
        calls.append(data)

    adapter = CallableAdapter(target)
    adapter.prepare(atheris)
    adapter.execute(b"abc")
    assert calls == [b"abc"]
    assert atheris.calls == [target]
```

(注:`test_protocol_conformance` 用 `TargetAdapter` 作 Protocol 时需 `runtime_checkable`;base.py 里加 `@runtime_checkable`。`/raise` 路由在 Flask 非测试模式下 `full_dispatch_request` 会返回 500 而不抛——为确保断言稳定,测试里先 `app.config["TESTING"] = True` 让异常传播。在 `_app()` 末尾加 `app.config["TESTING"] = True`。)

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest -q stateforge/tests/structural/test_adapters.py`
Expected: FAIL — `ModuleNotFoundError: stateforge.fuzz.adapters`

- [x] **Step 3: 实现三个文件**

`base.py`:
```python
"""Target adapter contract."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class TargetAdapter(Protocol):
    def prepare(self, atheris) -> None:
        """One-time setup before atheris.Fuzz(): instrumentation etc."""

    def execute(self, data: bytes) -> object:
        """Run one fuzz input against the target."""
```

`callable.py`:
```python
"""Adapter for plain ``target(data: bytes)`` callables."""

from __future__ import annotations

from typing import Callable

from stateforge.fuzz.instrumentation import Instrumenter


class CallableAdapter:
    def __init__(self, target: Callable[[bytes], object], instrument: bool = True):
        self.target = target
        self.instrument = instrument
        self._instrumenter = Instrumenter(registry=set())

    def prepare(self, atheris) -> None:
        if self.instrument:
            self._instrumenter.instrument_callable(self.target, atheris)

    def execute(self, data: bytes) -> object:
        return self.target(data)
```

`flask.py`:
```python
"""Flask adapter: fuzz bytes -> one full_dispatch_request."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable, Mapping

from stateforge.fuzz.instrumentation import Instrumenter

_PATH_PARAM = re.compile(r"<(?:int:|string:|path:|uuid:|float:)?\w+>")


@dataclass(frozen=True)
class RouteSpec:
    url: str
    endpoint: str
    method: str
    json_body: dict | None = None


class FlaskAdapter:
    """Enumerates routes, prefills path params, runs the full Flask stack.

    ``bodies`` maps endpoint -> dict or ``callable(data) -> dict``; fuzz bytes
    can drive request values through the callable form.
    """

    def __init__(self, app, headers: Mapping[str, str] | None = None,
                 bodies: Mapping[str, object] | None = None):
        self.app = app
        self.headers = dict(headers or {})
        self.bodies = dict(bodies or {})
        self.routes = self._enumerate()
        self._instrumenter = Instrumenter(registry=set())

    def _enumerate(self) -> list[RouteSpec]:
        routes = []
        for rule in self.app.url_map.iter_rules():
            if rule.endpoint == "static":
                continue
            methods = sorted(rule.methods - {"HEAD", "OPTIONS"})
            if not methods:
                continue
            method = "GET" if "GET" in methods else methods[0]
            routes.append(RouteSpec(_PATH_PARAM.sub("1", rule.rule),
                                    rule.endpoint, method))
        return routes

    def prepare(self, atheris) -> None:
        self._instrumenter.instrument_flask_app(self.app, atheris)

    def execute(self, data: bytes):
        if not self.routes or not data:
            return None
        route = self.routes[data[0] % len(self.routes)]
        body = self.bodies.get(route.endpoint)
        if callable(body):
            body = body(data)
        kwargs = {"method": route.method, "headers": self.headers}
        if body is not None:
            kwargs["json"] = body
        with self.app.test_request_context(route.url, **kwargs):
            return self.app.full_dispatch_request()
```

`__init__.py`:
```python
from stateforge.fuzz.adapters.base import TargetAdapter
from stateforge.fuzz.adapters.callable import CallableAdapter
from stateforge.fuzz.adapters.flask import FlaskAdapter, RouteSpec

__all__ = ["TargetAdapter", "CallableAdapter", "FlaskAdapter", "RouteSpec"]
```

- [x] **Step 4: 跑测试确认通过 + 全套绿**

Run: `python -m pytest -q stateforge/tests/structural/test_adapters.py` → 5 passed
Run: `python -m pytest -q stateforge/tests/` → 全绿

- [x] **Step 5: Commit**

```bash
git add stateforge/fuzz/adapters/ stateforge/tests/structural/test_adapters.py
git commit -m "T6-3:目标适配层 adapters/——base(Protocol)/callable/flask(full_dispatch_request+路径参数预填+bytes驱动body)

异常边界在 runner:adapter.execute 允许抛,runner 捕获记入 trace。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 4: vulnshop 适配工厂

**Files:**
- Create: `stateforge/fuzz/adapters/vulnshop.py`
- Test: `stateforge/tests/structural/test_vulnshop_adapter.py`

**Interfaces:**
- Consumes: `FlaskAdapter`(Task 3)、`stateforge.targets.vulnshop.app.create_app/make_token`
- Produces: `vulnshop_adapter(db_path, guards=0, username="alice", password="pw") -> FlaskAdapter`(11 路由全枚举、Bearer 头、五类 POST body 由 fuzz bytes 驱动)

- [x] **Step 1: 写失败测试**

```python
"""vulnshop adapter factory end-to-end (no atheris needed)."""
from pathlib import Path

from stateforge.fuzz.adapters.flask import FlaskAdapter
from stateforge.fuzz.adapters.vulnshop import vulnshop_adapter

ENDPOINTS = {"index", "register", "login", "me", "create_order", "read_order",
             "pay_order", "ship_order", "cancel_order", "read_coupon", "set_role"}


def test_vulnshop_adapter_covers_all_routes(tmp_path: Path):
    adapter = vulnshop_adapter(tmp_path / "v.db")
    assert isinstance(adapter, FlaskAdapter)
    assert {r.endpoint for r in adapter.routes} == ENDPOINTS


def test_all_seed_inputs_execute_without_escape(tmp_path: Path):
    adapter = vulnshop_adapter(tmp_path / "v.db")
    statuses = []
    for b in range(len(adapter.routes)):
        resp = adapter.execute(bytes([b]))
        statuses.append(None if resp is None else resp.status_code)
    assert all(s is not None for s in statuses)
    assert 200 in statuses          # happy path 至少可达


def test_post_bodies_driven_by_bytes(tmp_path: Path):
    adapter = vulnshop_adapter(tmp_path / "v.db")
    resp = adapter.execute(bytes([adapter._endpoint_index("create_order"), 5]))
    assert resp.status_code == 201  # items 合法,创建成功
    resp2 = adapter.execute(bytes([adapter._endpoint_index("pay_order"), 3]))
    assert resp2.status_code == 200
```

(`_endpoint_index` 若实现中不存在,测试改用 `next(i for i, r in enumerate(adapter.routes) if r.endpoint == X)`——实现时择一,保持一致。计划采用后者,测试写为:)

```python
def _idx(adapter, endpoint):
    return next(i for i, r in enumerate(adapter.routes) if r.endpoint == endpoint)
```

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest -q stateforge/tests/structural/test_vulnshop_adapter.py`
Expected: FAIL — `ModuleNotFoundError: stateforge.fuzz.adapters.vulnshop`

- [x] **Step 3: 实现**

```python
"""vulnshop target adapter: auth seed + byte-driven bodies."""

from __future__ import annotations

from stateforge.fuzz.adapters.flask import FlaskAdapter


def vulnshop_adapter(db_path, guards: int = 0,
                     username: str = "alice", password: str = "pw") -> FlaskAdapter:
    from stateforge.targets.vulnshop.app import create_app, make_token

    app = create_app(db_path=str(db_path), guards=guards, init_db=True)
    client = app.test_client()
    client.post("/auth/register", json={"username": username, "password": password})
    headers = {"Authorization": f"Bearer {make_token(username)}"}

    def _positive(data: bytes, default: int = 1) -> int:
        return data[1] if len(data) > 1 and data[1] > 0 else default

    bodies = {
        "register": lambda d: {"username": f"u{d[1] if len(d) > 1 else 0}",
                               "password": "pw"},
        "login": lambda d: {"username": username, "password": password},
        "create_order": lambda d: {"items": [{"price": _positive(d), "qty": 1}]},
        "pay_order": lambda d: {"amount": _positive(d)},
        "set_role": lambda d: {"role": "admin"},
    }
    return FlaskAdapter(app, headers=headers, bodies=bodies)
```

- [x] **Step 4: 跑测试确认通过 + 全套绿**

Run: `python -m pytest -q stateforge/tests/structural/test_vulnshop_adapter.py` → 3 passed
Run: `python -m pytest -q stateforge/tests/` → 全绿

- [x] **Step 5: Commit**

```bash
git add stateforge/fuzz/adapters/vulnshop.py stateforge/tests/structural/test_vulnshop_adapter.py
git commit -m "T6-4:vulnshop 适配工厂——预注册用户+Bearer、11 路由、fuzz bytes 驱动 POST body(price/amount/username)

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 5: JSONL 增量 trace(writer + loader)

**Files:**
- Modify: `stateforge/fuzz/atheris_runner.py`(新增 `TraceJsonlWriter`,保留现有函数)
- Modify: `stateforge/dynamic/io.py`(`load_events` 支持 JSONL)
- Test: `stateforge/tests/structural/test_trace_jsonl.py`

**Interfaces:**
- Consumes: `TraceEvent`(tracer)
- Produces:
  - `TraceJsonlWriter(path, fsync_every=100)`:`write_input(request_id, data, exception, events)`(逐行 flush,每 N 行 fsync)、`close()`。行格式:`{"request_id", "data_sha256", "data_length", "exception", "events": [TraceEvent dict...]}`
  - `load_events(path)`(扩展):识别 JSONL(首行是含 `request_id` 的对象)→ 逐行展平 events;原有 list/`{"events":...}` 格式行为不变

- [x] **Step 1: 写失败测试**

```python
"""JSONL trace writer + loader round trip."""
import json
from pathlib import Path

from stateforge.dynamic.io import load_events
from stateforge.dynamic.tracer import TraceEvent
from stateforge.fuzz.atheris_runner import TraceJsonlWriter


def test_writer_flushes_per_input_and_survives_no_close(tmp_path: Path):
    path = tmp_path / "trace.jsonl"
    writer = TraceJsonlWriter(path, fsync_every=2)
    writer.write_input(0, b"\x01", None, [TraceEvent(0, 0, "/a.py", "f", 1, "call")])
    # 不 close、直接重新打开读取(模拟进程被 _Exit:无任何清理)
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["request_id"] == 0
    assert rec["data_sha256"] and len(rec["data_sha256"]) == 64
    assert rec["data_length"] == 1
    assert rec["exception"] is None
    assert rec["events"][0]["function"] == "f"


def test_writer_records_exception_field(tmp_path: Path):
    writer = TraceJsonlWriter(tmp_path / "t.jsonl")
    writer.write_input(3, b"\xff", "ValueError: bad", [])
    rec = json.loads((tmp_path / "t.jsonl").read_text().splitlines()[0])
    assert rec["request_id"] == 3 and rec["exception"] == "ValueError: bad"


def test_load_events_reads_jsonl_in_order(tmp_path: Path):
    path = tmp_path / "trace.jsonl"
    writer = TraceJsonlWriter(path)
    writer.write_input(0, b"\x00", None, [TraceEvent(0, 0, "/a.py", "f", 1),
                                          TraceEvent(0, 1, "/a.py", "f", 2)])
    writer.write_input(1, b"\x01", None, [TraceEvent(1, 0, "/a.py", "g", 5)])
    writer.close()
    events = load_events(path)
    assert [(e.request_id, e.index, e.function) for e in events] == \
        [(0, 0, "f"), (0, 1, "f"), (1, 0, "g")]


def test_load_events_still_reads_legacy_formats(tmp_path: Path):
    # 旧格式一:裸数组
    p1 = tmp_path / "a.json"
    p1.write_text(json.dumps([{"request_id": 0, "index": 0, "file": "/a.py",
                               "function": "f", "line": 1, "event_type": "line"}]))
    assert load_events(p1)[0].function == "f"
    # 旧格式二:harness result 对象(顶层无 request_id)
    p2 = tmp_path / "b.json"
    p2.write_text(json.dumps({"data": "00", "exception": None,
                              "events": [{"request_id": 0, "index": 0, "file": "/a.py",
                                          "function": "f", "line": 1}]}))
    assert load_events(p2)[0].function == "f"
```

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest -q stateforge/tests/structural/test_trace_jsonl.py`
Expected: FAIL — `ImportError: cannot import name 'TraceJsonlWriter'`

- [x] **Step 3: 实现 writer(atheris_runner.py 顶部新增;`import hashlib, os` 加到 imports)**

```python
class TraceJsonlWriter:
    """Incremental per-input trace sink.

    libFuzzer terminates the process with _Exit after -runs/-max_total_time,
    bypassing finally/atexit.  Every input is flushed immediately (and fsynced
    every ``fsync_every`` lines) so the artifact survives hard termination.
    """

    def __init__(self, path: str | Path, fsync_every: int = 100):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fsync_every = fsync_every
        self._count = 0
        self._fh = self.path.open("w", encoding="utf-8")

    def write_input(self, request_id: int, data: bytes,
                    exception: str | None, events: list) -> None:
        record = {
            "request_id": request_id,
            "data_sha256": hashlib.sha256(data).hexdigest(),
            "data_length": len(data),
            "exception": exception,
            "events": [asdict(event) for event in events],
        }
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._count += 1
        self._fh.flush()
        if self.fsync_every and self._count % self.fsync_every == 0:
            os.fsync(self._fh.fileno())

    def close(self) -> None:
        try:
            os.fsync(self._fh.fileno())
        except OSError:
            pass
        finally:
            self._fh.close()
```

`io.py` 的 `load_events` 替换为:

```python
def load_events(path: str | Path) -> list[TraceEvent]:
    text = Path(path).read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text[0] == "[":
        raw: object = json.loads(text)
    elif text[0] == "{":
        first = json.loads(text.splitlines()[0])
        if "request_id" in first and "events" in first:      # JSONL:一行一输入
            events: list[TraceEvent] = []
            for line in text.splitlines():
                if line.strip():
                    events.extend(TraceEvent(**item)
                                  for item in json.loads(line)["events"])
            return events
        raw = json.loads(text)
    else:
        raise ValueError(f"unrecognized trace artifact: {path}")
    if isinstance(raw, dict):
        raw = raw.get("events", raw.get("trace", []))
    return [TraceEvent(**item) for item in raw]
```

- [x] **Step 4: 跑测试确认通过 + 全套绿(merge_events 兼容性由既有测试保障)**

Run: `python -m pytest -q stateforge/tests/structural/test_trace_jsonl.py` → 4 passed
Run: `python -m pytest -q stateforge/tests/` → 全绿

- [x] **Step 5: Commit**

```bash
git add stateforge/fuzz/atheris_runner.py stateforge/dynamic/io.py stateforge/tests/structural/test_trace_jsonl.py
git commit -m "T6-5:JSONL 增量 trace——逐输入落盘+flush/fsync(抗 libFuzzer _Exit);load_events 兼容 array/harness-object/JSONL 三格式

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 6: run_atheris 重写(adapter 化 + 共享 session + corpus 目录)

**Files:**
- Modify: `stateforge/fuzz/atheris_runner.py`(`run_atheris` 签名重写,新增 `build_input_handler`;其余函数不动)
- Test: `stateforge/tests/structural/test_runner_handler.py`

**Interfaces:**
- Consumes: `TargetAdapter`(Task 3)、`TraceSession.request()`(现有公共 API)、`TraceJsonlWriter`(Task 5)
- Produces:
  - `build_input_handler(adapter, session, writer=None, every_n=1) -> Callable[[bytes], None]`:每输入 `with session.request() as rid`(request_id 全局单调)、捕获异常为字符串、按 `rid % every_n` 采样写行
  - `run_atheris(adapter, root=None, argv=None, trace_path=None, corpus_dir=None, every_n=1, fsync_every=100) -> None`:`adapter.prepare(atheris)` → always-on `with session:` → `atheris.Setup/Fuzz`;`corpus_dir` 时 mkdir 并作为 libFuzzer 首个位置参数(种子由 libFuzzer 自己落盘,进程死后仍在)

- [x] **Step 1: 写失败测试(不需要 atheris:直接测 handler)**

```python
"""Input handler: monotonic request ids, exception capture, no cross-seed edges."""
from pathlib import Path

from stateforge.core.ir import ProgramIR
from stateforge.dynamic.mapper import map_trace
from stateforge.dynamic.tracer import TraceSession
from stateforge.fuzz.atheris_runner import TraceJsonlWriter, build_input_handler
from stateforge.static.icfg import build_icfg


class FakeAdapter:
    """First input runs outer_a, second runs outer_b — would fabricate an
    a->b edge under the old per-input request-id-reset behaviour."""

    def __init__(self, ns):
        self.ns = ns
        self.n = 0

    def prepare(self, atheris):
        pass

    def execute(self, data: bytes):
        self.n += 1
        (self.ns["outer_a"] if self.n == 1 else self.ns["outer_b"])(data)


SOURCE = (
    "def outer_a(d):\n"
    "    return leaf_a(d)\n"
    "def leaf_a(d):\n"
    "    return len(d)\n"
    "def outer_b(d):\n"
    "    return leaf_b(d)\n"
    "def leaf_b(d):\n"
    "    return len(d)\n"
)


def _per_request_edge_union(ir, session):
    union = set()
    for rid in sorted({e.request_id for e in session.events}):
        union |= {tuple(p) for p in map_trace(
            [e for e in session.events if e.request_id == rid], ir).edges}
    return union


def test_request_ids_monotonic_and_no_cross_seed_edges(tmp_path: Path):
    source = tmp_path / "app.py"
    source.write_text(SOURCE, encoding="utf-8")
    ns: dict = {}
    exec(compile(source.read_text(), str(source), "exec"), ns)
    ir: ProgramIR = build_icfg(tmp_path)

    session = TraceSession(tmp_path)
    writer = TraceJsonlWriter(tmp_path / "t.jsonl")
    handler = build_input_handler(FakeAdapter(ns), session, writer)
    handler(b"\x00")
    handler(b"\x01")

    rids = sorted({e.request_id for e in session.events})
    assert rids == [0, 1]                              # 单调,不重置
    whole = {tuple(p) for p in map_trace(session.events, ir).edges}
    assert whole == _per_request_edge_union(ir, session)   # 无跨种子伪造边


def test_handler_captures_exception_and_writes_line(tmp_path: Path):
    source = tmp_path / "app.py"
    source.write_text("def f(d):\n    raise ValueError('x')\n", encoding="utf-8")
    ns: dict = {}
    exec(compile(source.read_text(), str(source), "exec"), ns)

    class BoomAdapter:
        def prepare(self, atheris):
            pass

        def execute(self, data: bytes):
            ns["f"](data)

    session = TraceSession(tmp_path)
    writer = TraceJsonlWriter(tmp_path / "t.jsonl")
    handler = build_input_handler(BoomAdapter(), session, writer)
    handler(b"\x05")                                   # 不得抛出
    import json
    rec = json.loads((tmp_path / "t.jsonl").read_text().splitlines()[0])
    assert rec["exception"] == "ValueError: x"


def test_sampling_every_n_skips_lines(tmp_path: Path):
    source = tmp_path / "app.py"
    source.write_text("def f(d):\n    return len(d)\n", encoding="utf-8")
    ns: dict = {}
    exec(compile(source.read_text(), str(source), "exec"), ns)

    class SimpleAdapter:
        def prepare(self, atheris):
            pass

        def execute(self, data: bytes):
            ns["f"](data)

    session = TraceSession(tmp_path)
    writer = TraceJsonlWriter(tmp_path / "t.jsonl")
    handler = build_input_handler(SimpleAdapter(), session, writer, every_n=2)
    handler(b"\x00")
    handler(b"\x01")
    lines = (tmp_path / "t.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["request_id"] == 0
```

(文件顶部补 `import json`。)

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest -q stateforge/tests/structural/test_runner_handler.py`
Expected: FAIL — `ImportError: cannot import name 'build_input_handler'`

- [x] **Step 3: 实现(atheris_runner.py 中旧 `run_atheris` 整体替换)**

```python
def build_input_handler(adapter, session, writer: TraceJsonlWriter | None = None,
                        every_n: int = 1):
    """Per-input boundary: fresh request id, exception capture, trace line."""
    def handle(data: bytes) -> None:
        with session.request() as rid:
            start = len(session.events)
            error: str | None = None
            try:
                adapter.execute(data)
            except Exception as exc:      # fuzz inputs must not stop the engine
                error = f"{type(exc).__name__}: {exc}"
            if writer is not None and rid % every_n == 0:
                writer.write_input(rid, data, error, session.events[start:])
    return handle


def run_atheris(adapter, root: str | Path | None = None,
                argv: list[str] | None = None, trace_path: str | Path | None = None,
                corpus_dir: str | Path | None = None, every_n: int = 1,
                fsync_every: int = 100) -> None:
    """Drive Atheris over a TargetAdapter with structural tracing.

    Atheris owns mutation, scheduling, execution, and native coverage
    (via adapter.prepare instrumentation).  StateForge only records the
    ordered Python trace per input.  ``corpus_dir`` is handed to libFuzzer
    as its corpus directory so seeds persist across the process _Exit.
    """
    import atheris
    from stateforge.dynamic.tracer import TraceSession

    adapter.prepare(atheris)
    session = TraceSession(root)
    writer = TraceJsonlWriter(trace_path, fsync_every) if trace_path else None
    handler = build_input_handler(adapter, session, writer, every_n)
    fuzz_argv = list(sys.argv if argv is None else argv)
    if corpus_dir:
        Path(corpus_dir).mkdir(parents=True, exist_ok=True)
        fuzz_argv = [fuzz_argv[0], str(corpus_dir), *fuzz_argv[1:]]
    atheris.Setup(fuzz_argv, handler)
    try:
        with session:
            atheris.Fuzz()
    finally:
        if writer is not None:
            writer.close()      # best-effort; per-line flush already durable
```

- [x] **Step 4: 跑测试确认通过 + 全套绿**

Run: `python -m pytest -q stateforge/tests/structural/test_runner_handler.py` → 3 passed
Run: `python -m pytest -q stateforge/tests/` → 全绿

- [x] **Step 5: Commit**

```bash
git add stateforge/fuzz/atheris_runner.py stateforge/tests/structural/test_runner_handler.py
git commit -m "T6-6:run_atheris 重写——adapter 化、共享 TraceSession(request_id 单调,修复跨种子伪造边)、异常捕获、corpus 目录由 libFuzzer 自落盘、every_n 采样

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 7: 验收实验(6 gates)+ README 更新

**Files:**
- Create: `stateforge/fuzz/acceptance.py`(可复跑的验收驱动:`python -m stateforge.fuzz.acceptance [runs|max_total_time] [--anota]`)
- Modify: `stateforge/README.md`(fuzz 节替换)
- Test: 无新单测;验收即测试

**Interfaces:**
- Consumes: 全部前序产物
- Produces: 验收数字记入本文件末尾"验收记录"节;README 反映新边界与 corpus prioritization 定位

- [ ] **Step 1: 写验收驱动 acceptance.py**

```python
"""T+6 acceptance driver.  Usage:
    PYTHONPATH=/home/landasika/icassp python -m stateforge.fuzz.acceptance [args]
libFuzzer args pass through, e.g. `-runs=500` or `-max_total_time=10`.
"""
from __future__ import annotations

import sys

from stateforge.fuzz.adapters.vulnshop import vulnshop_adapter
from stateforge.fuzz.atheris_runner import run_atheris

ROOT = "/home/landasika/icassp/stateforge/targets/vulnshop"


def main() -> int:
    fuzz_args = [a for a in sys.argv[1:] if a.startswith("-")] or ["-runs=500"]
    out = "/tmp/sf-t6"
    run_atheris(vulnshop_adapter(f"{out}/vulnshop.db"),
                root=ROOT,
                argv=["acceptance", "-verbosity=1", *fuzz_args],
                trace_path=f"{out}/trace.jsonl",
                corpus_dir=f"{out}/corpus")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: G1 插桩闭环(系统 py,合成函数)**

临时脚本(不进 repo):
```python
import sys
sys.path.insert(0, "/home/landasika/icassp")
from stateforge.fuzz.adapters.callable import CallableAdapter
from stateforge.fuzz.atheris_runner import run_atheris

def branchy(data: bytes) -> None:
    n = data[0] if data else 0
    if n % 3 == 0:
        n += 1
    if n % 5 == 1:
        n += 2
    if n > 200:
        n = 0

run_atheris(CallableAdapter(branchy),
            argv=["g1", "-runs=500", "-verbosity=1"],
            trace_path="/tmp/sf-t6/g1.jsonl")
```
Run + gate:`timeout 120 python /tmp/g1.py 2>&1 | grep -E 'cov:' | tail -3`
Expected: **cov > 1 且出现 NEW 行**(插桩+闭环恢复;改造前此形态必出 `no interesting inputs`)

- [ ] **Step 3: G2/G3 vulnshop 覆盖(系统 py + ANOTA py)**

Run: `timeout 180 env PYTHONPATH=/home/landasika/icassp python -m stateforge.fuzz.acceptance -runs=500 2>&1 | grep -E 'NEW.*cov:|Done|ERROR' | tail -5`
Expected: cov 持续增长、corpus 增长、无 `no interesting inputs` ERROR

Run: `timeout 60 anota/ANOTA/ANOTA-interpreter/python -m stateforge.fuzz.acceptance -max_total_time=15 2>&1 | grep -E 'NEW.*cov:|Done|ERROR' | tail -5`
Expected: 同上(ANOTA 下闭环)

- [ ] **Step 4: G4 trace 存活 + feedback 可读(进程 _Exit 后)**

Run:
```bash
wc -l /tmp/sf-t6/trace.jsonl && ls /tmp/sf-t6/corpus | head -3
PYTHONPATH=/home/landasika/icassp python -m stateforge.cli feedback \
  /tmp/sf-smoke/static/program_ir.json /tmp/sf-t6/trace.jsonl \
  --out /tmp/sf-t6/feedback.json
```
Expected: trace.jsonl 行数 > 0(fuzz 结束即存在);corpus 目录有种子文件;feedback 命令 exit 0 且 frontiers > 0
(注:program_ir.json 若 /tmp/sf-smoke 已清,先 `python -m stateforge.cli analyze stateforge/targets/vulnshop --out /tmp/sf-smoke/static` 重建。)

- [ ] **Step 5: G5 无跨种子边(JSONL 全量过 mapper)**

```bash
python - <<'EOF'
import sys; sys.path.insert(0, "/home/landasika/icassp")
from stateforge.core.ir import ProgramIR
from stateforge.dynamic.io import load_events
from stateforge.dynamic.mapper import map_trace
ir = ProgramIR.from_json("/tmp/sf-smoke/static/program_ir.json")
events = load_events("/tmp/sf-t6/trace.jsonl")
whole = {tuple(e) for e in map_trace(events, ir).edges}
union = set()
for rid in {e.request_id for e in events}:
    union |= {tuple(e) for e in map_trace([e for e in events if e.request_id == rid], ir).edges}
assert whole == union, f"cross-seed edges: {len(whole - union)}"
print(f"OK: {len(events)} events, {len({e.request_id for e in events})} requests, no cross-seed edges")
EOF
```
Expected: `OK: ... no cross-seed edges`

- [ ] **Step 6: G6 量化对齐(新旧同预算对比,ANOTA py,15s)**

```bash
git show HEAD~6:stateforge/fuzz/atheris_harness.py > /tmp/old_harness.py 2>/dev/null || \
  git show $(git log --oneline --all -S 'atheris_harness' --format=%h | head -1):stateforge/fuzz/atheris_harness.py > /tmp/old_harness.py
timeout 60 anota/ANOTA/ANOTA-interpreter/python /tmp/old_harness.py stateforge/targets/vulnshop -max_total_time=15 2>&1 | grep -oE 'cov: [0-9]+' | tail -1
timeout 60 anota/ANOTA/ANOTA-interpreter/python -m stateforge.fuzz.acceptance -max_total_time=15 2>&1 | grep -oE 'cov: [0-9]+' | tail -1
```
(HEAD~6 不可靠时的兜底:基线 commit 之前的 `0b5c093` 里有该文件:`git show 0b5c093:stateforge/fuzz/atheris_harness.py`。)
Gate:**新 cov ≥ 旧 cov × 0.9**,且新栈 exec/s( Done 行)记录在案、≥ 100(系统 py;ANOTA 记录不设硬门槛,因为 tracer 开销在 ANOTA 上待测)。

- [ ] **Step 7: 更新 README fuzz 节**

`stateforge/README.md` 的 "Python integration" 节替换为:

```markdown
## Python integration

The Atheris boundary is adapter-based:

```python
from stateforge.fuzz.adapters.vulnshop import vulnshop_adapter
from stateforge.fuzz.atheris_runner import run_atheris

run_atheris(vulnshop_adapter("/tmp/vulnshop.db"),
            root="stateforge/targets/vulnshop",
            argv=["runner", "-max_total_time=30"],
            trace_path="/tmp/trace.jsonl",     # per-input JSONL, survives _Exit
            corpus_dir="/tmp/corpus")          # libFuzzer persists seeds itself
```

- Atheris owns mutation, scheduling, execution, and **native coverage**
  (the adapter's `prepare` selectively instruments views via
  `stateforge.fuzz.instrumentation`; `instrument_all` is never used).
- StateForge records the ordered Python trace per input (shared
  `TraceSession`, globally monotonic request ids, incremental JSONL).
- `stateforge.feedback.corpus.evaluate_seed` + `atheris_runner.rank_corpus`
  produce **inter-round corpus prioritization metadata** — StateForge does
  not modify Atheris's internal scheduler; ranking is applied between
  fuzzing rounds.
```

- [ ] **Step 8: 全套回归 + 双解释器 anota 套件**

Run: `python -m pytest -q stateforge/tests/` → 全绿
Run: `anota/ANOTA/ANOTA-interpreter/python -m pytest -q -m anota stateforge/tests` → 33 passed(与本会话基线一致)

- [ ] **Step 9: 验收数字写回本计划末尾 + Commit**

在本文件末尾追加"## 验收记录"一节:G1–G6 各自的实际数字(cov、corpus 数、exec/s、trace 行数、事件数)。
```bash
git add stateforge/fuzz/acceptance.py stateforge/README.md plans/T6-atheris-boundary.md
git commit -m "T6-7:验收实验 6 gates 全过 + README(适配器边界/JSONL/corpus prioritization 定位)

G1 插桩闭环 cov=…;G2/G3 双解释器覆盖增长;G4 trace _Exit 存活+feedback 可读;G5 零跨种子边;G6 新/旧 cov …/…(≥90%)。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Self-Review 结果

- **Spec 覆盖**:Codex 十点——①两层边界(Task 3/6)②插桩 P0(Task 2)③Flask 分发(Task 3)④共享 session(Task 6)⑤hook 保存恢复(现有代码已有,行为保持)⑥JSONL P0(Task 5)⑦sha256 不存原文(Task 5)⑧corpus prioritization 措辞(Task 7 README)⑨文件布局(Task 2-6)⑩五步顺序+验收(Task 7)。补强五处——tracer 热路径(Task 1)、保留策略 every_n(Task 6)、corpus 目录传入(Task 6)、JSONL loader(Task 5)、量化验收 G6(Task 7)。无缺口。
- **占位符扫描**:Task 7 Step 6 的 `git show HEAD~6` 给了兜底命令(`0b5c093:`),非 TBD;无其他占位。
- **类型一致性**:`build_input_handler(adapter, session, writer=None, every_n=1)` 在 Task 6 定义并在验收脚本中以默认参数使用;`TraceJsonlWriter(path, fsync_every=100)` 两处一致;`Instrumenter(registry=set())` 的隔离用法在 Task 2 测试与 Task 3 实现一致;`FlaskAdapter.execute` 返回 `Response | None`,vulnshop 测试依赖此返回值。
