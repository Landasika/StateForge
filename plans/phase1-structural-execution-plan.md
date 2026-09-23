# Phase-1 结构执行图收尾 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 `plans/phase1-structural-execution-graph.md` 补齐结构执行图的八个真实缺口(P1-1..P1-8),交付可信的 ICFG + BB/edge 覆盖 + frontier 链路,`/buy` fixture 端到端验收。

**Architecture:** 全部改动落在既有结构管线上(`static/` 构图、`dynamic/` 采集映射、`feedback/` 归算、`cli.py` 入口);旧 XFG 语义管线(`analyzer/cfg.py`)通过委托 `static/cfg.py` 修复块归属,语义层数据结构零改动。

**Tech Stack:** 纯 Python stdlib(ast/dataclasses/json);测试 driver 侧 `python3 -m pytest`(anaconda py3.13,flask 3.1.0/pytest 8.3.4);不引入新依赖。

**Spec:** `plans/phase1-structural-execution-graph.md`(本计划从该 spec 论证;执行者两个文件都要读)

## Global Constraints

- 测试命令一律在仓库根 `python3 -m pytest <path> -v`;pytest.ini 默认 `-m "not anota"`,结构测试**不得**依赖 ANOTA 解释器。
- **PHASE-1 黑名单**(spec §7,全文以 spec 为准):禁止删除/重构 StateCandidate/StateVar/Effect/Precondition/Guard/Sink/Invariant/SSTG/snapshot/planner/executor/LLM provider/auth_baseline;禁止删 attach_anchors/state-link/anchor_param_influence/block_gates;禁止全项目格式化;禁止改 AppModel schema;禁止伪造 CALL 边;禁止为过测试硬编码目标业务信息。
- ProgramIR schema 只做**增量**(新 kind、新字段均带默认值;`from_dict` 对旧 JSON 向后兼容)。
- 删除文件数必须为 0。每个任务红→绿→commit,禁止跨任务攒批。
- 提交信息末尾带 `Co-Authored-By: Claude Code <noreply@anthropic.com>`。

---

### Task 1: P1-1 ownership-aware 调用归属

修复 `static/icfg.py` 嵌套函数/lambda 内的 call 被误归属到外层块。

**Files:**
- Modify: `stateforge/static/icfg.py`(call 收集处,当前 60-64 行)
- Test: `stateforge/tests/static/test_icfg_ownership.py`(新建)

**Interfaces:**
- Produces: `_iter_owned_calls(fn_node: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterator[ast.Call]`(模块级函数,Task 2 复用);行为:遍历函数体但**不下钻** `FunctionDef/AsyncFunctionDef/Lambda/ClassDef` 的体。

- [ ] **Step 1: 写失败测试**

```python
"""P1-1:嵌套函数/lambda 内的调用不得归属到外层函数的块。"""
import ast
from pathlib import Path

from stateforge.static.cfg import build_function_cfg
from stateforge.static.icfg import _iter_owned_calls, build_icfg

SRC = '''\
def outer(x):
    helper(x)
    def inner(y):
        helper(y)
        return other(y)
    f = lambda z: helper(z)
    return inner(x)
'''
````

```python
def _tmp(tmp_path: Path, src: str) -> Path:
    d = tmp_path / "app"
    d.mkdir()
    (d / "app.py").write_text(src, encoding="utf-8")
    return d


def test_nested_def_calls_not_owned():
    tree = ast.parse(SRC)
    outer = tree.body[0]
    calls = [c for c in _iter_owned_calls(outer)]
    names = [c.func.id for c in calls if isinstance(c.func, ast.Name)]
    assert names == ["helper"]          # inner/lambda 里的 helper/other 不算 outer 的


def test_icfg_nested_function_separate():
    # 完整工程:inner 是独立 FunctionIR,outer 的块不因 inner 体里的 helper 而多出 call 边
    pass  # 见 Step 1b
```

- [ ] **Step 1b: 补工程级断言(失败测试的第二半)**

```python
def test_icfg_nested_call_not_in_outer_blocks(tmp_path):
    root = _tmp(tmp_path, SRC + "\n\ndef helper(v):\n    return v\n")
    ir = build_icfg(root)
    outer = ir.functions["app.outer"]
    call_srcs = [e.src for e in ir.edges if e.kind == "call"]
    outer_block_calls = [b for b in outer.blocks if b in call_srcs]
    # outer 只有一个直接调用(helper),inner 里的调用挂在 app.inner 的块上
    assert len(outer_block_calls) <= 1
    assert "app.inner" in ir.functions
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest stateforge/tests/static/test_icfg_ownership.py -v`
Expected: FAIL,`ImportError: cannot import name '_iter_owned_calls'`

- [ ] **Step 3: 实现**

`stateforge/static/icfg.py` 顶部(import 之后)加:

```python
_OWNED_STOP = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _iter_owned_calls(fn_node):
    """Yield ast.Call nodes belonging to *fn_node* itself.

    Ownership: never descend into nested function/lambda/class bodies —
    those belong to their own FunctionIR (spec P1-1).  The root function's
    own decorators are outside its body and thus skipped.
    """
    stack = list(fn_node.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Call):
            yield node
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, _OWNED_STOP):
                stack.append(child)
```

把 60-64 行的

```python
            calls = [n for n in ast.walk(node)
                     if isinstance(n, ast.Call) and getattr(n, "lineno", 0) in lines]
```

换成(注意:`ast_functions` 的 key 是 `(module, fname)`,函数重名时 `node` 可能是别的函数——沿用现状,本任务不扩大):

```python
            calls = [n for n in _iter_owned_calls(node)
                     if getattr(n, "lineno", 0) in lines]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest stateforge/tests/static/test_icfg_ownership.py -v`
Expected: 3 passed

- [ ] **Step 5: 全量回归 + 提交**

Run: `python3 -m pytest stateforge/tests -q`
Expected: 全绿(结构测试不依赖旧断言数量;若有失败,仅允许因归属修正导致的计数变化,更新断言并在提交信息说明)

```bash
git add stateforge/static/icfg.py stateforge/tests/static/test_icfg_ownership.py
git commit -m "P1-1: icfg 调用归属改 ownership-aware 遍历,嵌套函数/lambda 不再误归属外层块"
```

---

### Task 2: P1-2 同块多 call 保序(CS/RS 合成节点)

**Files:**
- Modify: `stateforge/static/icfg.py`(call/return 边构造整段重写,当前 57-91 行)
- Modify: `stateforge/core/ir.py`(仅注释:BasicBlock.kind 文档补 `callsite`/`returnsite`)
- Test: `stateforge/tests/static/test_icfg_callsite_chain.py`(新建)

**Interfaces:**
- Consumes: `_iter_owned_calls`(Task 1);`FlowEdge(src, dst, kind, condition=None, callsite_line=None, callee=None)`;`FunctionIR(entry, exit, blocks)`。
- Produces: 块 kind `callsite`/`returnsite`;块 ID 格式 `{file}::{func}::{line}-{line}::callsite::{n}` / `...::returnsite::{n}`(**n 为函数内序号,先 CS 后 RS 同一 n 配对**);边序列 `B→CS#i(flow)`、`CS#i→callee.entry(call)`、`callee.exit→RS#i(return)`、`RS#N→succ(B)(flow)`;`RS#N` 的后继为 B 的原 normal 后继(无后继时 → `caller.exit`)。B 的原 `B→succ` 边被移除(链替代)。ProgramIR 新增方法 `remove_edge(src, dst, kind)`。

- [ ] **Step 1: 写失败测试**

```python
"""P1-2:同块多 call 的 CS/RS 合成链保序;return helper() 特判。"""
from pathlib import Path

from stateforge.static.icfg import build_icfg

MULTI = '''\
def handler(u):
    a = first(u)
    b = second(a)
    return b

def first(x):
    return x

def second(y):
    return y
'''

TAIL = '''\
def handler(u):
    return helper(u)

def helper(x):
    return x
'''


def _root(tmp_path, src):
    d = tmp_path / "app"
    d.mkdir()
    (d / "app.py").write_text(src, encoding="utf-8")
    return d


def test_chain_order_and_kinds(tmp_path):
    ir = build_icfg(_root(tmp_path, MULTI))
    h = ir.functions["app.handler"]
    cs = [b for b in h.blocks if ir.blocks[b].kind == "callsite"]
    rs = [b for b in h.blocks if ir.blocks[b].kind == "returnsite"]
    assert len(cs) == 2 and len(rs) == 2
    first_cs, second_cs = sorted(cs, key=lambda b: ir.blocks[b].start_line)
    # B → CS1 → first.entry; first.exit → RS1 → CS2 → second.entry
    assert ir.outgoing(first_cs, "call")[0].dst == ir.functions["app.first"].entry
    assert ir.incoming(ir.functions["app.first"].exit, "return")
    # 同块两个 call 顺序:first 的 return 落点能 flow 到第二个 call 的 CS
    rs_by_callee = {e.callsite_line: e.src for e in ir.edges if e.kind == "return"}
    assert rs_by_callee  # return 边的 src = callee.exit
    # 原 B→succ 直边被链替代:handler 内不再有绕过 CS 的 block→block flow 穿过调用
    body = [b for b in h.blocks if ir.blocks[b].kind == "block"]
    assert body, "handler 应有顺序块"
    b_succs = [e.dst for e in ir.outgoing(body[0], "flow")]
    assert b_succs == [first_cs]


def test_tail_call_returns_to_exit(tmp_path):
    ir = build_icfg(_root(tmp_path, TAIL))
    h = ir.functions["app.handler"]
    rs = [b for b in h.blocks if ir.blocks[b].kind == "returnsite"]
    assert len(rs) == 1
    ret_edges = ir.outgoing(rs[0], "flow")
    assert [e.dst for e in ret_edges] == [h.exit]


def test_no_fake_call_edge(tmp_path):
    ir = build_icfg(_root(tmp_path, TAIL + "\n\ndef ghost():\n    return 0\n"))
    # ghost 无人调用:不出现任何 src→ghost.entry 的 call 边,也不进 unresolved(方向是 caller 侧)
    ge = ir.functions["app.ghost"].entry
    assert not any(e.dst == ge and e.kind == "call" for e in ir.edges)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest stateforge/tests/static/test_icfg_callsite_chain.py -v`
Expected: FAIL(callsite 块不存在/断言失败)

- [ ] **Step 3: 实现**

`core/ir.py` 的 ProgramIR 增加方法(在 `add_edge` 后):

```python
    def remove_edge(self, src: str, dst: str, kind: str) -> None:
        self.edges = [e for e in self.edges
                      if not (e.src == src and e.dst == dst and e.kind == kind)]
```

`core/ir.py` 的 `BasicBlock.kind` 注释行改为 `kind: str = "block"  # block/branch/terminal/entry/exit/loop_exit/try/callsite/returnsite`。

`static/icfg.py`:整段替换当前 call 解析循环(从 `unresolved: list[dict[str, object]] = []` 到 `ir.metadata["unresolved_calls"] = unresolved` 之前的边构造部分)。新实现:

```python
    unresolved: list[dict[str, object]] = []
    for qname, fn in list(ir.functions.items()):
        module, _, fname = qname.rpartition(".")
        node = ast_functions.get((module, fname))
        if node is None:
            continue
        # 收集本函数各块的内调用,按语句行号排序(块内保序)
        block_calls: dict[str, list[tuple[int, str]]] = {}
        for call in _iter_owned_calls(node):
            callee_short = _callee_name(call)
            callee = _resolve_callee(callee_short, module, ast_functions, aliases)
            line = getattr(call, "lineno", 0)
            if callee is None or callee not in ir.functions or callee == qname:
                unresolved.append({"caller": qname, "callee": callee_short, "line": line})
                continue
            host = next((bid for bid in fn.blocks
                         if line in ir.blocks[bid].line_numbers), None)
            if host is None:
                unresolved.append({"caller": qname, "callee": callee_short,
                                   "reason": "no host block", "line": line})
                continue
            block_calls.setdefault(host, []).append((line, callee))

        for host, calls in block_calls.items():
            calls.sort(key=lambda t: t[0])
            succs = [e.dst for e in ir.outgoing(host)
                     if e.kind in ("flow", "true", "false")]
            continuations = succs or [fn.exit]
            for dst in succs:                      # 原 B→succ 直边由链替代
                ir.remove_edge(host, dst, "flow")
                ir.remove_edge(host, dst, "true")
                ir.remove_edge(host, dst, "false")
            prev = host
            for n, (line, callee) in enumerate(calls, start=1):
                file = ir.blocks[host].file
                cs = f"{file}::{fname}::{line}-{line}::callsite::{n}"
                rs = f"{file}::{fname}::{line}-{line}::returnsite::{n}"
                ir.blocks[cs] = BasicBlock(cs, file, module, fname, line, line,
                                           "callsite", [f"<call> {callee}"], [line])
                ir.blocks[rs] = BasicBlock(rs, file, module, fname, line, line,
                                           "returnsite", [f"<ret> {callee}"], [line])
                ir.add_edge(FlowEdge(prev, cs, "flow", callsite_line=line, callee=callee))
                ir.add_edge(FlowEdge(cs, ir.functions[callee].entry, "call",
                                     callsite_line=line, callee=callee))
                ir.add_edge(FlowEdge(ir.functions[callee].exit, rs, "return",
                                     callsite_line=line, callee=callee))
                prev = rs
            for dst in continuations:
                ir.add_edge(FlowEdge(prev, dst, "flow"))
            fn.blocks.extend([b for b in ir.blocks
                              if ir.blocks[b].kind in ("callsite", "returnsite")
                              and ir.blocks[b].function == fname
                              and ir.blocks[b].file == ir.blocks[host].file])
    ir.metadata["unresolved_calls"] = unresolved
```

注意:imports 行补 `from stateforge.core.ir import ProgramIR, FlowEdge, BasicBlock`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest stateforge/tests/static/test_icfg_callsite_chain.py -v`
Expected: 3 passed

- [ ] **Step 5: 全量回归 + 提交**

Run: `python3 -m pytest stateforge/tests -q`
Expected: 全绿(coverage/frontier 测试如有计数基线变化,按新语义更新并说明)

```bash
git add stateforge/static/icfg.py stateforge/core/ir.py stateforge/tests/static/test_icfg_callsite_chain.py
git commit -m "P1-2: 同块多 call 保序——CS/RS 合成节点链替代 B→succ 直边,return 配对到明确 returnsite"
```

---

### Task 3: mapper 对齐 CS/RS(动态 call/return 走合成链)

**Files:**
- Modify: `stateforge/dynamic/mapper.py`
- Test: `stateforge/tests/dynamic/test_map_callsite.py`(新建;`tests/dynamic/` 目录与 `__init__.py` 一并建)

**Interfaces:**
- Consumes: 块 kind `callsite`/`returnsite`(Task 2);`TraceEvent(request_id, index, file, function, line, event_type)`。
- Produces: `map_trace(events, ir)` 行为变更——call 事件经 CS 落到 callee.entry,return 事件后先落 RS 再继续;`MappedTrace` 结构不变。

- [ ] **Step 1: 写失败测试**

```python
"""P1-2/P1-3:动态 call→CS→callee.entry、callee.exit→RS 的映射。"""
from pathlib import Path

from stateforge.dynamic.mapper import map_trace
from stateforge.dynamic.tracer import TraceEvent
from stateforge.static.icfg import build_icfg

SRC = '''\
def handler(u):
    a = first(u)
    b = second(a)
    return b

def first(x):
    return x

def second(y):
    return y
'''


def _run(tmp_path):
    d = tmp_path / "app"
    d.mkdir()
    (d / "app.py").write_text(SRC, encoding="utf-8")
    ir = build_icfg(d)
    f = str(d / "app.py")
    ev = [
        TraceEvent(0, 0, f, "handler", 2, "line"),
        TraceEvent(0, 1, f, "first", 1, "call"),
        TraceEvent(0, 2, f, "first", 2, "line"),
        TraceEvent(0, 3, f, "first", 2, "return"),
        TraceEvent(0, 4, f, "handler", 3, "line"),
        TraceEvent(0, 5, f, "second", 1, "call"),
        TraceEvent(0, 6, f, "second", 2, "line"),
        TraceEvent(0, 7, f, "second", 2, "return"),
        TraceEvent(0, 8, f, "handler", 4, "line"),
    ]
    return ir, map_trace(ev, ir)


def test_call_passes_through_callsite(tmp_path):
    ir, mt = _run(tmp_path)
    kinds = {b: ir.blocks[b].kind for b in mt.blocks}
    seq = [b for b in mt.blocks if kinds[b] in ("callsite", "returnsite")]
    # 至少:CS(first) → [first] → RS(first) → CS(second) → [second] → RS(second)
    kseq = [kinds[b] for b in seq]
    assert kseq == ["callsite", "returnsite", "callsite", "returnsite"]


def test_return_edge_kinds_present(tmp_path):
    ir, mt = _run(tmp_path)
    ret_pairs = [p for p, ks in mt.edge_kinds.items() if "return" in ks]
    assert ret_pairs, "应观察到 return 边(callee.exit→RS)"
    call_pairs = [p for p, ks in mt.edge_kinds.items() if "call" in ks]
    assert call_pairs
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest stateforge/tests/dynamic/test_map_callsite.py -v`
Expected: FAIL(kseq 为空——当前 call 直接 pair 到 callee.entry)

- [ ] **Step 3: 实现**

`dynamic/mapper.py`:`map_trace` 内加静态索引与调用栈(在 `out = MappedTrace()` 后):

```python
    # P1-2 对齐:caller块→callee.entry 的静态链经 CS;return 落 RS
    cs_for: dict[tuple[str, str], str] = {}          # (caller_block, callee_entry) -> cs
    rs_of: dict[str, str] = {}                       # cs -> rs
    for edge in ir.edges:
        if edge.kind == "call" and ir.blocks[edge.src].kind == "callsite":
            feeder = ir.incoming(edge.src, "flow")
            if feeder:
                cs_for[(feeder[0].src, edge.dst)] = edge.src
                back = ir.outgoing(edge.src, "flow")  # 占位,RS 由 return 边反查
    for edge in ir.edges:
        if edge.kind == "return" and ir.blocks[edge.dst].kind == "returnsite":
            cs_line = edge.callsite_line
            cs = next((e.src for e in ir.outgoing("", "call")
                       if False), None)  # 由下面直接建映射
    for edge in ir.edges:                             # cs -> rs:同 callsite_line 且同 caller
    # (简化实现:按 callee.exit 的 return 边落点即 RS;CS 由 cs_for 反查)
    for edge in ir.edges:
        if edge.kind == "return" and ir.blocks[edge.dst].kind == "returnsite":
            pass
```

上面草稿不采用。**最终采用**(把整段替换为干净实现):

```python
    cs_for: dict[tuple[str, str], str] = {}          # (caller_block, callee_entry) -> cs
    rs_for_cs: dict[str, str] = {}                   # cs -> rs
    for edge in ir.edges:
        if edge.kind != "call" or ir.blocks.get(edge.src, None) is None:
            continue
        if ir.blocks[edge.src].kind != "callsite":
            continue
        feeders = [e for e in ir.edges
                   if e.dst == edge.src and e.kind == "flow"
                   and ir.blocks.get(e.src) is not None
                   and ir.blocks[e.src].kind not in ("callsite", "returnsite")]
        if not feeders:
            continue
        cs_for[(feeders[0].src, edge.dst)] = edge.src
        pair = next((e for e in ir.edges
                     if e.kind == "return" and e.callsite_line == edge.callsite_line
                     and e.callee == edge.callee), None)
        if pair is not None:
            rs_for_cs[edge.src] = pair.src if False else pair.dst
```

再修事件循环(替换 `for event in events:` 体中 call/return 分支;`call_stack` 在 `previous_request` 重置处一并清空):

```python
    call_stack: list[str] = []                        # 待落 RS 的栈(cs id)
    ...
    for event in events:
        if previous_request is not None and event.request_id != previous_request:
            previous = None
            call_stack = []
        ...
        if event.event_type == "call" and function is not None:
            cs = cs_for.get((previous, function.entry)) if previous else None
            if cs is not None:
                append_block(cs)
                call_stack.append(cs)
            append_block(function.entry)
            continue
        if event.event_type == "return" and function is not None:
            append_block(function.exit)
            if call_stack:
                cs = call_stack.pop()
                rs = rs_for_cs.get(cs)
                if rs is not None:
                    append_block(rs)
            continue
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest stateforge/tests/dynamic/test_map_callsite.py -v`
Expected: 2 passed

- [ ] **Step 5: 回归 + 提交**

Run: `python3 -m pytest stateforge/tests -q`

```bash
git add stateforge/dynamic/mapper.py stateforge/tests/dynamic/
git commit -m "P1-2: mapper 动态 call/return 对齐 CS/RS 合成链(调用栈逐请求清空)"
```

---

### Task 4: P1-3 tracer 补 exception 事件

**Files:**
- Modify: `stateforge/dynamic/tracer.py:43`
- Test: `stateforge/tests/dynamic/test_trace_exception.py`(新建)

**Interfaces:**
- Produces: `TraceEvent.event_type == "exception"`(mapper 不映射、仅入 `unresolved`——本任务不改 mapper)。

- [ ] **Step 1: 写失败测试**

```python
"""P1-3:tracer 采集 exception 事件。"""
from stateforge.dynamic.tracer import TraceSession


def test_exception_event_recorded():
    with TraceSession() as session:
        with session.request() as rid:
            try:
                raise ValueError("boom")
            except ValueError:
                pass
    ev_types = [e.event_type for e in session.events]
    assert "exception" in ev_types
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest stateforge/tests/dynamic/test_trace_exception.py -v`
Expected: FAIL(白名单不含 exception,事件被丢弃)

- [ ] **Step 3: 实现**

`tracer.py:43` 白名单改:`if event not in {"call", "line", "return", "exception"}:`

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest stateforge/tests/dynamic/test_trace_exception.py -v`
Expected: PASS

- [ ] **Step 5: 回归 + 提交**

Run: `python3 -m pytest stateforge/tests -q`(mapper 现有测试确认 exception 事件落 unresolved 不炸)

```bash
git add stateforge/dynamic/tracer.py stateforge/tests/dynamic/test_trace_exception.py
git commit -m "P1-3: tracer 增采 exception 事件(动态属性,不映射静态边)"
```

---

### Task 5: P1-4 覆盖的 request 维度

**Files:**
- Modify: `stateforge/dynamic/mapper.py`(MappedTrace 增字段 + 逐事件记账)
- Modify: `stateforge/feedback/overlay.py`
- Modify: `stateforge/feedback/engine.py`
- Modify: `stateforge/cli.py`(feedback payload)
- Test: `stateforge/tests/feedback/test_request_counts.py`(新建;`tests/feedback/__init__.py` 一并建)

**Interfaces:**
- Produces: `MappedTrace.block_requests: dict[str, set[int]]`、`MappedTrace.edge_requests: dict[tuple[str, str], set[int]]`;`Overlay.block_request_count: dict[str, int]`、`Overlay.edge_request_count: dict[tuple[str, str], int]`;`FeedbackReport.request_count: int`;payload 键 `request_count`/`block_request_count`。

- [ ] **Step 1: 写失败测试**

```python
"""P1-4:块/边按请求维度的计数。"""
from collections import Counter
from stateforge.core.ir import ProgramIR, BasicBlock, FunctionIR, FlowEdge
from stateforge.dynamic.mapper import MappedTrace, map_trace
from stateforge.dynamic.tracer import TraceEvent
from stateforge.feedback.engine import compute_feedback
from stateforge.feedback.overlay import build_overlay


def _ir():
    ir = ProgramIR()
    f = "/t/app.py"
    ir.blocks["b1"] = BasicBlock("b1", f, "app", "h", 1, 2, "block", [], [1, 2])
    ir.blocks["b2"] = BasicBlock("b2", f, "app", "h", 3, 4, "block", [], [3, 4])
    ir.functions["app.h"] = FunctionIR("app.h", f, "app", "b1", "b2", ["b1", "b2"])
    ir.edges.append(FlowEdge("b1", "b2", "flow"))
    return ir


def _ev(rid, line):
    return TraceEvent(rid, rid * 10 + line, "/t/app.py", "h", line, "line")


def test_block_request_count_two_requests():
    ir = _ir()
    events = [_ev(0, 1), _ev(0, 3), _ev(1, 1)]        # 请求 0 走 b1→b2,请求 1 只走 b1
    mt = map_trace(events, ir)
    assert mt.block_requests["b1"] == {0, 1}
    assert mt.block_requests["b2"] == {0}
    ov = build_overlay(ir, mt)
    assert ov.block_request_count["b1"] == 2
    assert ov.edge_request_count[("b1", "b2")] == 1
    report = compute_feedback(ir, events)
    assert report.request_count == 2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest stateforge/tests/feedback/test_request_counts.py -v`
Expected: FAIL,MappedTrace 无 block_requests

- [ ] **Step 3: 实现**

`mapper.py` MappedTrace 增字段:

```python
    block_requests: dict[str, set[int]] = field(default_factory=dict)
    edge_requests: dict[tuple[str, str], set[int]] = field(default_factory=dict)
```

`append_block` 内(hit 记账处)补:

```python
        rid_now = previous_request if previous_request is not None else -1
        out.block_requests.setdefault(hit, set()).add(rid_now)
        if previous is not None:
            out.edge_requests.setdefault(pair, set()).add(rid_now)
```

(`pair` 在已有代码中已构造;把 `rid_now` 的取值放在 `previous_request` 赋值之后。)

`overlay.py`:

```python
@dataclass
class Overlay:
    covered_blocks: set[str]
    covered_edges: set[tuple[str, str]]
    block_hits: dict[str, int]
    edge_hits: dict[tuple[str, str], int]
    block_request_count: dict[str, int]
    edge_request_count: dict[tuple[str, str], int]


def build_overlay(ir, trace):
    return Overlay(set(trace.block_hits), set(trace.edge_hits),
                   dict(trace.block_hits), dict(trace.edge_hits),
                   {b: len(r) for b, r in trace.block_requests.items()},
                   {e: len(r) for e, r in trace.edge_requests.items()})
```

`engine.py` FeedbackReport 增 `request_count: int`;`compute_feedback` 末尾:

```python
    return FeedbackReport(overlay, frontiers, distances, mapped, len(ir.blocks),
                          request_count=len({e.request_id for e in events}))
```

`cli.py` feedback payload 增:

```python
        "request_count": report.request_count,
        "block_request_count": report.overlay.block_request_count,
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest stateforge/tests/feedback/test_request_counts.py -v`
Expected: PASS

- [ ] **Step 5: 回归 + 提交**

Run: `python3 -m pytest stateforge/tests -q`

```bash
git add stateforge/dynamic/mapper.py stateforge/feedback/overlay.py stateforge/feedback/engine.py stateforge/cli.py stateforge/tests/feedback/
git commit -m "P1-4: 覆盖增加 request 维度——MappedTrace/Overlay/FeedbackReport/cli payload"
```

---

### Task 6: P1-5 静态+动态叠图 viz

**Files:**
- Create: `stateforge/viz/__init__.py`、`stateforge/viz/coverage_dot.py`
- Modify: `stateforge/cli.py`(viz 子命令)
- Test: `stateforge/tests/viz/test_coverage_dot.py`(新建;含 `__init__.py`)

**Interfaces:**
- Consumes: `ProgramIR.from_json`;Task 5 的 feedback payload 键。
- Produces: `coverage_dot(ir: ProgramIR, feedback: dict, path: str | Path) -> Path`;cli:`python -m stateforge.cli viz <ir.json> <feedback.json> [--out out.dot]`。

- [ ] **Step 1: 写失败测试**

```python
"""P1-5:viz 冒烟——dot 生成、覆盖着色、frontier 高亮。"""
from stateforge.core.ir import ProgramIR, BasicBlock, FunctionIR, FlowEdge
from stateforge.viz.coverage_dot import coverage_dot


def _ir():
    ir = ProgramIR()
    f = "/t/app.py"
    ir.blocks["b1"] = BasicBlock("b1", f, "app", "h", 1, 2, "block", [], [1, 2])
    ir.blocks["b2"] = BasicBlock("b2", f, "app", "h", 3, 4, "block", [], [3, 4])
    ir.functions["app.h"] = FunctionIR("app.h", f, "app", "b1", "b2", ["b1", "b2"])
    ir.edges.append(FlowEdge("b1", "b2", "true"))
    return ir


def test_dot_smoke(tmp_path):
    fb = {"covered_blocks": ["b1"], "frontiers": [{"src": "b1", "dst": "b2",
                                                   "kind": "true"}],
          "block_hits": {"b1": 3}}
    out = coverage_dot(_ir(), fb, tmp_path / "cov.dot")
    text = out.read_text(encoding="utf-8")
    assert text.startswith("digraph")
    assert 'fillcolor="palegreen"' in text      # 覆盖块着色
    assert 'color="gray"' in text               # 未覆盖块灰
    assert "red" in text and "frontier" in text  # frontier 边红
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest stateforge/tests/viz/test_coverage_dot.py -v`
Expected: FAIL,ModuleNotFoundError stateforge.viz

- [ ] **Step 3: 实现**

`stateforge/viz/coverage_dot.py`:

```python
"""Static+dynamic coverage overlay as a Graphviz dot file."""

from __future__ import annotations

from pathlib import Path

from stateforge.core.ir import ProgramIR

_KIND_SHAPE = {"branch": "diamond", "entry": "circle", "exit": "doublecircle",
               "terminal": "box, style=rounded", "callsite": "point",
               "returnsite": "point", "loop_exit": "point", "try": "ellipse"}


def coverage_dot(ir: ProgramIR, feedback: dict, path: str | Path) -> Path:
    hits = feedback.get("block_hits", {})
    covered = set(feedback.get("covered_blocks", []))
    frontier = {(f["src"], f["dst"]) for f in feedback.get("frontiers", [])}
    lines = ["digraph Coverage {", "  rankdir=LR;", '  node [fontname="Helvetica"];']
    for bid, block in ir.blocks.items():
        shape = _KIND_SHAPE.get(block.kind, "rect")
        if bid in covered:
            lines.append(f'  "{bid}" [shape={shape}, style=filled, '
                         f'fillcolor="palegreen", label="{bid}\\n hits={hits.get(bid, 0)}"];')
        else:
            lines.append(f'  "{bid}" [shape={shape}, color="gray", '
                         f'fontcolor="gray", label="{bid}"];')
    for edge in ir.edges:
        if edge.kind not in {"flow", "true", "false", "call", "return"}:
            continue
        if (edge.src, edge.dst) in frontier:
            lines.append(f'  "{edge.src}" -> "{edge.dst}" '
                         '[color="red", penwidth=2.5, label="frontier"];')
        else:
            label = edge.kind if edge.kind in ("call", "return") else ""
            attr = f' [label="{label}"]' if label else ""
            color = ' color="purple"' if edge.kind in ("call", "return") else ""
            lines.append(f'  "{edge.src}" -> "{edge.dst}"{color}{attr};')
    lines.append("}")
    out = Path(path)
    out.write_text("\n".join(lines), encoding="utf-8")
    return out
```

`cli.py` main() 增子命令(import 处加 `import json` 已有、`from stateforge.viz.coverage_dot import coverage_dot` 放函数内避免全局依赖):

```python
    viz = sub.add_parser("viz", help="static+dynamic coverage overlay dot")
    viz.add_argument("ir")
    viz.add_argument("feedback")
    viz.add_argument("--out", default="results/coverage.dot")
    ...
    if args.command == "viz":
        from stateforge.viz.coverage_dot import coverage_dot
        ir_obj = ProgramIR.from_json(args.ir)
        fb = json.loads(Path(args.feedback).read_text(encoding="utf-8"))
        out = coverage_dot(ir_obj, fb, args.out)
        print(f"[viz] {out}")
        return 0
```

(挂在现有 `if args.command == ...` 链中;`experiment` 不动。)

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest stateforge/tests/viz/test_coverage_dot.py -v`
Expected: PASS

- [ ] **Step 5: 回归 + 提交**

Run: `python3 -m pytest stateforge/tests -q`

```bash
git add stateforge/viz/ stateforge/cli.py stateforge/tests/viz/
git commit -m "P1-5: coverage_dot 叠图(覆盖着色/frontier 红)与 cli viz 子命令"
```

---

### Task 7: P1-6 旧管线委托 static/cfg.py(锚点归属修复)

**Files:**
- Modify: `stateforge/analyzer/cfg.py`(`build_cfg` 重写为委托包装;`build_xfg` 调用点消 fallback)
- Test: 跑既有 `stateforge/tests/analyzer/test_cfg.py`(不新增文件;行为修复由既有断言+新增回归用例覆盖,新增用例写进该文件末尾)

**Interfaces:**
- Consumes: `static/cfg.py build_function_cfg(node, file, module) -> FunctionCFG`;`FunctionCFG.function/blocks/edges`;`_iter_owned_calls` 不直接用,委托后 `_block_calls_name` 因 stmts 归属正确而自动修复。
- Produces: `build_cfg(func|Endpoint, prefix, file) -> nx.DiGraph` **契约不变**——节点属性 `kind ∈ {entry,exit,block,branch,terminal,missing}`、`stmts: list[ast 模块级 AST 对象]`(branch 块为 `[test 表达式]`)、`lines: (lo, hi)`、`file/func/anchors`;边属性 `cond ∈ {"T","F","back","break","continue",None}`;`terminal` 属性在 terminal 块上为 True。新增行为:break/continue 不再当死终点(连 loop_exit/header);`if admin: delete_user()` 的 callsite 不在 branch 块。

- [ ] **Step 1: 在 test_cfg.py 末尾追加失败回归用例**

```python
ADMIN = """
def handler(admin):
    if admin:
        delete_user()
    return "done"
"""


def test_call_not_owned_by_branch_block():
    g = _cfg_of(ADMIN)
    branch = next(n for n, d in g.nodes(data=True) if d.get("kind") == "branch")
    body = [n for n, d in g.nodes(data=True)
            if d.get("kind") == "block" and d["lines"][0] > g.nodes[branch]["lines"][1]]
    assert body, "delete_user() 应有自己的顺序块"
    # branch 块只拥有 test(admin),不拥有 delete_user 调用
    branch_text = "; ".join(ast.unparse(s) for s in g.nodes[branch]["stmts"])
    assert "delete_user" not in branch_text


def test_break_continue_wired():
    src = ("def f(items):\n    for x in items:\n"
           "        if x:\n            break\n        process(x)\n    return 0\n")
    g = _cfg_of(src)
    brk = [n for n, d in g.nodes(data=True)
           if d.get("stmts") and getattr(d["stmts"][0], "lineno", None)
           and "break" in ast.unparse(d["stmts"][0])]
    assert brk, "break 语句成块"
    assert any(g.successors(brk[0])), "break 不再是死终点,应连向 loop 出口"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest stateforge/tests/analyzer/test_cfg.py -v`
Expected: 新增 2 用例 FAIL(现状:branch 拥有整个 If;break 死终点)

- [ ] **Step 3: 实现 build_cfg 委托**

`analyzer/cfg.py` 顶部 import 增:`from stateforge.static.cfg import build_function_cfg`。`build_cfg` 整体替换(保留 Endpoint 分派与 missing 分支):

```python
def build_cfg(func: ast.FunctionDef | Endpoint, prefix: str = "",
              file: str | None = None) -> nx.DiGraph:
    """过程内 CFG(委托 static/cfg.py,语义修复:P1-1/P1-2 的归属与控制流)。

    契约(消费方 attach_anchors/_block_calls_name/数据流层/旧测试):
    节点 kind/stmts(AST 对象,branch 块=[test 表达式])/lines(lo,hi)/terminal;
    边 cond ∈ {"T","F","back","break","continue",None}。
    """
    if isinstance(func, Endpoint):
        file = func.file
        fdef = _locate_func(func.file, func.func)
        if fdef is None:
            g = nx.DiGraph()
            g.add_node(f"{prefix}missing", kind="missing", func=func.func)
            return g
        func = fdef
    file = file or "<ast>"
    module = Path(file).resolve().stem or "app"

    built = build_function_cfg(func, file, module)
    g = nx.DiGraph()
    for bid, blk in built.blocks.items():
        g.add_node(bid, kind=blk.kind, stmts=_ast_bodies_for(func, blk),
                   lines=(blk.start_line, blk.end_line), file=file,
                   func=func.name, anchors=[])
    cond_map = {"true": "T", "false": "F", None: None,
                "back": "back", "break": "break", "continue": "continue"}
    for edge in built.edges:
        g.add_edge(edge.src, edge.dst, cond=cond_map.get(edge.condition, edge.condition))
    for n, d in g.nodes(data=True):
        if d.get("kind") == "terminal":
            d["terminal"] = True
    return g


def _ast_bodies_for(func: ast.FunctionDef, blk) -> list:
    """把 AST 语句/条件表达式重新挂到委托块上(ownership-aware,不下钻嵌套定义)。"""
    import stateforge.analyzer.cfg as _self  # noqa: PLC0415  延迟避免循环
    owned: list = []
    stack = list(func.body)
    while stack:
        node = stack.pop(0)
        if isinstance(node, (ast.If, ast.While, ast.For, ast.AsyncFor)):
            test = getattr(node, "test", None) or getattr(node, "iter", None)
            if test is not None and test.lineno in blk.line_numbers \
                    and blk.kind == "branch":
                owned.append(test)
        elif node.lineno in blk.line_numbers and blk.kind in ("block", "terminal"):
            owned.append(node)
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                      ast.Lambda, ast.ClassDef)):
                stack.append(child)
    return owned
```

`build_xfg` 的 fallback 行(当前 284 行):

```python
            for s in sites or [f"{cmod}.{cname}::{next(iter(cfg_c.nodes))}"]:
                xfg.add_edge(s, target, kind=kind, callee=g)
```

替换为(找不到 callsite 就跳过并计数,不造假边):

```python
            if not sites:
                xfg.graph.setdefault("unresolved_calls", 0)
                xfg.graph["unresolved_calls"] += 1
                continue
            for s in sites:
                xfg.add_edge(s, target, kind=kind, callee=g)
```

- [ ] **Step 4: 跑旧测试全绿(含 goldens)**

Run: `python3 -m pytest stateforge/tests/analyzer -v`
Expected: 全部 PASS。若 `test_golden_vulnshop.py` 因归属修正产生 diff:检查 diff 是否全部属于"调用归属从 branch 块移到 body 块 / break 出环"这两类修正;是则用 `pytest --force-golden` 或等价再生机制更新(无该机制则手写更新 golden JSON 并在提交信息逐条列出变化原因);任何其他类别 diff 停下来报告,不得强改。

- [ ] **Step 5: 全量回归 + 提交**

Run: `python3 -m pytest stateforge/tests -q`

```bash
git add stateforge/analyzer/cfg.py stateforge/tests/analyzer/test_cfg.py stateforge/targets/vulnshop/golden/ 2>/dev/null || git add stateforge/analyzer/cfg.py stateforge/tests/analyzer/test_cfg.py
git commit -m "P1-6: build_cfg 委托 static/cfg.py——锚点/调用归属修复,break/continue 正确连边,build_xfg 消除首块 fallback"
```

---

### Task 8: P1-7 source_hash 缓存标识

**Files:**
- Modify: `stateforge/static/icfg.py`(metadata)
- Modify: `stateforge/cli.py`(feedback 加载时校验)
- Test: `stateforge/tests/static/test_source_hash.py`(新建)

**Interfaces:**
- Produces: `ir.metadata["source_hash"]`(sha256,输入为按路径排序的 `path:size:mtime` 行);cli feedback 在 ir 根目录可重算且不一致时 payload 增 `"ir_stale": true` 并 stderr 警告。

- [ ] **Step 1: 写失败测试**

```python
"""P1-7:source_hash 存在且内容变化后失效。"""
import time

from stateforge.static.icfg import build_icfg, _source_hash


def _app(tmp_path):
    d = tmp_path / "app"
    d.mkdir()
    (d / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    return d


def test_hash_present_and_changes(tmp_path):
    d = _app(tmp_path)
    ir = build_icfg(d)
    assert ir.metadata["source_hash"] == _source_hash(d)
    p = d / "app.py"
    st = p.stat()
    p.write_text("def f():\n    return 2\n", encoding="utf-8")
    import os
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    assert _source_hash(d) != ir.metadata["source_hash"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest stateforge/tests/static/test_source_hash.py -v`
Expected: FAIL,无 `_source_hash`

- [ ] **Step 3: 实现**

`static/icfg.py`:

```python
import hashlib


def _source_hash(root: Path) -> str:
    parts = []
    for path in _files(root):
        st = path.stat()
        parts.append(f"{path}:{st.st_size}:{st.st_mtime_ns}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()
```

`build_icfg` 里 `ir = ProgramIR(metadata={...})` 处补一项(在 return 前也可,选构造后统一):

```python
    ir.metadata["source_hash"] = _source_hash(root)
```

`cli.py` feedback_cmd 在 `ir = ProgramIR.from_json(ir_path)` 后:

```python
    ir_stale = False
    root = ir.metadata.get("root")
    if root:
        from pathlib import Path as _P
        from stateforge.static.icfg import _source_hash
        try:
            ir_stale = _source_hash(_P(root)) != ir.metadata.get("source_hash")
        except OSError:
            ir_stale = True
        if ir_stale:
            print("[warn] ir is stale: sources changed since analyze", file=sys.stderr)
```

payload dict 增 `"ir_stale": ir_stale,`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest stateforge/tests/static/test_source_hash.py -v`
Expected: PASS

- [ ] **Step 5: 回归 + 提交**

Run: `python3 -m pytest stateforge/tests -q`

```bash
git add stateforge/static/icfg.py stateforge/cli.py stateforge/tests/static/test_source_hash.py
git commit -m "P1-7: program_ir 带 source_hash,cli feedback 陈旧告警(ir_stale)"
```

---

### Task 9: P1-8 /buy 验收 fixture + 端到端

**Files:**
- Create: `stateforge/tests/fixtures/__init__.py`、`stateforge/tests/fixtures/buy_app/__init__.py`、`stateforge/tests/fixtures/buy_app/app.py`
- Create: `stateforge/tests/static/test_buy_acceptance.py`

**Interfaces:**
- Consumes: 全部前置任务;`dynamic/tracer.TraceSession`(直接 sys.settrace,不经 atheris);flask `test_client`(driver 侧 flask 3.1.0 可用);`cli.py experiment`。
- Produces: 验收结论(spec §4 P1-8 的四条断言)。

- [ ] **Step 1: fixture 应用**

`buy_app/app.py`:

```python
"""Phase-1 acceptance fixture(spec §4 P1-8)。同块双调用 + 分支调用。"""
from flask import Flask


def create_app():
    app = Flask(__name__)
    app.config["USERS"] = {"bee": type("U", (), {"balance": 0})()}

    @app.post("/buy")
    def buy():
        user = get_user()
        log_access()
        if not user:
            return "unauthorized", 401
        return checkout(user)

    return app


def get_user():
    from flask import request
    return request.headers.get("X-User") or None


def log_access():
    return None


def checkout(user):
    if user.balance > 100:
        return charge(user)
    return "insufficient"


def charge(user):
    return "charged"
```

- [ ] **Step 2: 写验收测试(预期全绿,若有红即 Phase-1 未达标)**

`test_buy_acceptance.py`:

```python
"""Phase-1 验收:静态链路 + 动态 balance=50 请求 + frontier(spec §4 P1-8)。"""
import importlib.util
import json
from pathlib import Path

from stateforge.dynamic.io import merge_events
from stateforge.dynamic.tracer import TraceSession
from stateforge.feedback.engine import compute_feedback
from stateforge.static.icfg import build_icfg

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "buy_app"


def _load_app():
    spec = importlib.util.spec_from_file_location("buy_app_app", FIXTURE / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.create_app()


def _trace_one_request(client, user_balance):
    app_mod = None
    import buy_app_app  # noqa: F401  已注入 sys.modules?否——直接 set 属性
    raise SystemExit("placeholder 由实现期替换")
```

(实现期按下面最终形态写,不要保留占位;最终测试:)

```python
def _trace_buy(balance):
    import sys
    spec = importlib.util.spec_from_file_location("buy_app_app", FIXTURE / "app.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["buy_app_app"] = mod
    spec.loader.exec_module(mod)
    app = mod.create_app()
    app.config["USERS"]["bee"].balance = balance
    client = app.test_client()
    events = []
    with TraceSession(root=str(FIXTURE)) as session:
        with session.request() as rid:
            client.post("/buy", headers={"X-User": "bee"})
            events = list(session.events)
    return events


def test_static_chain():
    ir = build_icfg(FIXTURE)
    fns = set(ir.functions)
    assert {"buy_app_app.buy", "buy_app_app.get_user", "buy_app_app.log_access",
            "buy_app_app.checkout", "buy_app_app.charge"} <= fns
    kinds = {(e.src, e.dst, e.kind) for e in ir.edges}
    cs = [b for b in ir.blocks.values() if b.kind == "callsite"]
    rs = [b for b in ir.blocks.values() if b.kind == "returnsite"]
    assert len(cs) >= 4 and len(rs) >= 4     # buy×2 + checkout×1(+内部)
    assert any(k == "return" for _, _, k in kinds)


def test_dynamic_frontier():
    ir = build_icfg(FIXTURE)
    events = _trace_buy(50)                   # 余额不足:只走 false 分支
    report = compute_feedback(ir, events)
    assert report.overlay.covered_blocks
    # checkout 条件块的 true 边未走 → frontier 恰含该边
    frontier_pairs = {(f.src, f.dst) for f in report.frontiers}
    assert frontier_pairs, "balance=50 时 true 分支必为 frontier"
    for src, dst in frontier_pairs:
        assert ir.blocks[src].function == "checkout"


def test_cli_experiment_e2e(tmp_path):
    import subprocess
    import sys as _sys
    out = tmp_path / "exp"
    trace = tmp_path / "trace.jsonl"
    # 采集一条请求的 trace 落 JSONL(复用 TraceSession 序列化)
    events = _trace_buy(50)
    trace.write_text("\n".join(json.dumps({
        "request_id": e.request_id, "index": e.index, "file": e.file,
        "function": e.function, "line": e.line, "event_type": e.event_type,
    }) for e in events), encoding="utf-8")
    r = subprocess.run([_sys.executable, "-m", "stateforge.cli", "experiment",
                        str(FIXTURE), "--out", str(out), "--trace", str(trace)],
                       capture_output=True, text=True, check=True)
    fb = json.loads((out / "feedback.json").read_text(encoding="utf-8"))
    assert fb["frontiers"] and fb["request_count"] >= 1
```

注意:`cli experiment` 的 `feedback` 命令用 `merge_events` 读 trace——确认 `dynamic/io.load_events` 兼容上面逐行 JSON(是:JSONL 三格式兼容为 T6-5 已交付)。若 `experiment` 子命令的 `--trace` 传参路径与现有 argparse 不符,按 `cli.py` 现状调整测试调用方式(读 `cli.py` main)。

- [ ] **Step 3: 跑验收**

Run: `python3 -m pytest stateforge/tests/static/test_buy_acceptance.py -v`
Expected: 3 passed。**若 test_dynamic_frontier 失败:按 systematic-debugging 流程定位(通常是 runtime co_filename 与静态解析路径不一致——`_files(root)` 产出绝对路径,fixture 用 `FIXTURE` 绝对路径,二者应一致;或 flask 内部帧被 root 过滤是预期行为),禁止为过测试改断言语义。**

- [ ] **Step 4: 全量回归**

Run: `python3 -m pytest stateforge/tests -q`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add stateforge/tests/fixtures/ stateforge/tests/static/test_buy_acceptance.py
git commit -m "P1-8: /buy 验收 fixture——静态 CS/RS 链 + 动态 balance=50 frontier + cli experiment 端到端"
```

---

### Task 10: 收尾——spec 状态更新 + 限制记录

**Files:**
- Modify: `plans/phase1-structural-execution-graph.md`(状态行:spec 定稿 → 已实施;§10 补实测发现的过近似)
- Modify: `stateforge/static/` 下新增 `LIMITATIONS-P1.md`(若实现中出现新过近似;无则跳过此文件)

**Interfaces:** 无代码。

- [ ] **Step 1: 全量测试最终跑**

Run: `python3 -m pytest stateforge/tests -q`
Expected: 全绿;记录用例数

- [ ] **Step 2: 更新 spec 状态行与限制**

把 spec 首行 `状态:spec 定稿(2026-09-23),待审。` 改为 `状态:已实施(<日期>,commit <hash>);验收见 tests/static/test_buy_acceptance.py。`;§10 若实现中发现新的过近似(如 mapper 对嵌套调用帧的 edge_kinds 归并),逐条补入。

- [ ] **Step 3: 提交**

```bash
git add plans/phase1-structural-execution-graph.md
git commit -m "Phase-1 收尾:spec 状态转已实施,记录实测限制"
```

---

## Self-Review 结论

- **Spec 覆盖:** P1-1→Task 1;P1-2→Task 2+3;P1-3→Task 4;P1-4→Task 5;P1-5→Task 6;P1-6→Task 7;P1-7→Task 8;P1-8→Task 9;黑名单/NON-GOALS→Global Constraints 与 Task 10;§4 P1-6 的 goldens 处理→Task 7 Step 4。无缺口。
- **占位符:** Task 9 Step 2 中间那段带 `raise SystemExit("placeholder")` 的草稿已标注"不保留、以最终形态为准",最终形态代码紧随其后完整给出;其余无 TBD。
- **类型一致性:** `block_requests/edge_requests`(mapper)→`block_request_count/edge_request_count`(overlay)→`request_count`(engine/payload)各任务一致;CS/RS ID 格式与 kind 在 Task 2/3/6 一致;`remove_edge` 在 Task 2 定义、仅 Task 2 使用。
