"""P0-A 统一执行上下文 —— 测试输入级状态隔离边界。

背景(阶段 1 任务书):``entry_driver.execute_sequence`` 的请求序列有
进程内持久会话连续性(钉扎语义,tests/fuzzing/test_sequence_seeds.py::
test_state_persists_across_libfuzzer_inputs_until_reset),但没有定义
"一个测试输入开始/结束"的状态生命周期;``TraceSession.request()`` 只
隔离追踪事件收集范围,不等于应用状态隔离。本模块把输入级生命周期
显式化为 ``ExecutionContext`` + ``StateLifecycle`` 后端:

- ``NoopLifecycle``:旧实验兼容(输入间状态延续),``isolation="none"``
  显式标记**没有隔离**——不是"隔离了但没生效"。
- ``ResetLifecycle``:微基准/内存态靶——每输入**前**调用靶 reset 钩子
  (模块约定名 ``reset_state``,与 plan_executor/goal_directed_eval
  同源约定),把应用拉回基线。
- ``SnapshotLifecycle``:Flask/SQLite 等文件态靶——首个输入前捕获文件
  基线(L2Sqlite 位级模板拷贝),每输入**后**关连接→gc→还原文件→
  重置内存钩子。复用 ``runtime/snapshot/l2_sqlite``(ADR-0004:位级
  模板拷贝 + journal/-wal/-shm 清理);fork-COW 引擎
  (engine/supervisor/client)是**跨进程、按 epoch 服务 HTTP** 的机制,
  生命周期与进程内 ``target(data)`` 调用不匹配——故只复用其 L2 文件
  原语,不包装引擎(阅读 engine.py/protocol.py/supervisor.py/
  l2_sqlite.py 后的裁决)。

隔离粒度 = 测试输入(一条 libFuzzer input / 一条序列种子),不是单请求:
序列内前序请求写入的状态对后续请求**可见**(验收条件 3);隔离发生在
输入边界,不破坏序列语义。

失败语义(fail-closed):begin/还原失败 → ``LifecycleError``,**不静默
继续执行**(否则实验被污染);上下文置 poisoned,后续 run 一律拒绝,
直到进程重启。调用方(collector/off handler)捕获后跳过该输入并写
``lifecycle_failures.jsonl`` 审计——fuzz 引擎不 crash,但该输入不计
执行、不计覆盖、不入语料。观测器(observer)失败**不** poison:证据
缺失 ≠ 状态污染,只计数审计(显式退化,不冒充)。

分层(L2 runtime):仅依赖 stdlib + runtime/snapshot;l2_sqlite 之外
零本包依赖(observer 经 TYPE_CHECKING 鸭子类型,不 import state 域)。
"""

from __future__ import annotations

import gc
import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Protocol, runtime_checkable

from stateforge.runtime.snapshot.l2_sqlite import (L2Sqlite,
                                                 SnapshotConsistencyError)

if TYPE_CHECKING:                                    # 仅类型标注(L1.5 不上引)
    from stateforge.state.runtime_observer import RuntimeObserver, StateObservation


class LifecycleError(RuntimeError):
    """隔离后端失败:不得静默继续(实验完整性优先于可用性)。"""


ISOLATION_NONE = "none"
ISOLATION_RESET = "reset"
ISOLATION_SNAPSHOT = "snapshot"

# 靶模块钩子约定名(P0-A/P0-B 的靶侧声明面;详见模块 docstring):
RESET_HOOK_ATTR = "reset_state"        # 既有约定(plan_executor/goal_directed_eval 同源)
SNAPSHOT_FILES_ATTR = "SF_STATE_FILES"   # 文件态靶声明基线文件(list[str|Path])
MEMORY_RESET_ATTR = "SF_MEMORY_RESET"    # 内存态恢复钩子(→ establish 时刻)
OBSERVER_ATTR = "sf_state_observer"      # 靶侧观测器工厂;缺 = 无运行时观测


# ------------------------------------------------------------- 生命周期后端

@runtime_checkable
class StateLifecycle(Protocol):
    name: str
    isolation: str

    def establish_baseline(self) -> None: ...
    def begin_run(self) -> None: ...
    def end_run(self) -> None: ...


class NoopLifecycle:
    """旧语义:无隔离(输入间状态延续)。永不失败、永不 raise。"""

    name = "noop"
    isolation = ISOLATION_NONE

    def establish_baseline(self) -> None:
        pass

    def begin_run(self) -> None:
        pass

    def end_run(self) -> None:
        pass


def _fail(hook: Callable[[], Any], what: str) -> None:
    """钩子执行 → 失败统一包装为 LifecycleError(不静默)。"""
    try:
        hook()
    except LifecycleError:
        raise
    except Exception as exc:                          # noqa: BLE001
        raise LifecycleError(f"{what} 失败({getattr(hook, '__name__', hook)}): "
                             f"{exc}") from exc


class ResetLifecycle:
    """每输入前调用 reset 钩子拉回基线(内存态靶/微基准)。

    基线由钩子自定义(如微靶 reset_state 清 _SESSION/_DB/HITS);钩子
    失败 = 隔离失败 → LifecycleError(fail-closed,不许带着脏态执行)。
    """

    def __init__(self, reset_hooks: Iterable[Callable[[], None]]):
        self.reset_hooks = list(reset_hooks)
        if not self.reset_hooks:
            raise ValueError("ResetLifecycle 至少需要一个 reset 钩子")
        self.name = "reset"
        self.isolation = ISOLATION_RESET

    def establish_baseline(self) -> None:
        pass                                          # 基线语义由钩子自持

    def begin_run(self) -> None:
        for hook in self.reset_hooks:
            _fail(hook, "reset 钩子")

    def end_run(self) -> None:
        pass


class SnapshotLifecycle:
    """文件态基线快照:首输入前捕获,每输入后还原。

    复用 L2Sqlite(位级模板拷贝;-journal/-wal/-shm 清理)。还原顺序
    (任务书裁决,顺序不可换):

        1. pre_restore_hooks —— 关闭持有的 DB 连接(**文件覆盖前**,
           连接仍开着就覆盖 .db 是被明确禁止的);
        2. gc.collect() —— 兜底回收未被显式关闭的 sqlite3 连接
           (CPython refcount 之外的悬挂引用);
        3. L2Sqlite.restore —— 模板位拷贝回 live + 副本文件清理;
        4. memory_hooks —— 内存态恢复到 establish 时刻(如符号绑定表)。

    已知边界:WAL 模式下未 checkpoint 的页在 -wal 里,基线捕获必须在
    静默点(vulnshop 是 journal_mode=DELETE,无此问题);模板丢失 =
    基线丢失 → LifecycleError(L2Sqlite.restore 对缺失模板静默返回,
    此处显式收口,防"看起来还原了"的假隔离)。
    """

    BASELINE = "baseline"

    def __init__(self, files: Iterable[str | Path] = (),
                 memory_hooks: Iterable[Callable[[], None]] = (),
                 pre_restore_hooks: Iterable[Callable[[], None]] = (),
                 gc_before_restore: bool = True):
        self.files = [Path(f) for f in files]
        self.memory_hooks = list(memory_hooks)
        self.pre_restore_hooks = list(pre_restore_hooks)
        self.gc_before_restore = gc_before_restore
        if not self.files and not self.memory_hooks:
            raise ValueError("SnapshotLifecycle 需要 files 或 memory_hooks 之一")
        self.name = "snapshot"
        self.isolation = ISOLATION_SNAPSHOT
        # 每文件独立模板根(同目录多 DB 不互踩;不落在 live 同名旁污染靶目录枚举)
        self._l2 = [L2Sqlite(f, tpl_root=f.parent / f".sf_tpl_{f.name}")
                    for f in self.files]
        self._established = False

    def establish_baseline(self) -> None:
        """捕获当前文件态为基线(在首个输入前、应用干净时调用)。

        PR-02:capture=backup(缺省)经 SQLite backup API——WAL 已提交
        未 checkpoint 帧自动入模板;sqlite3.Error(锁冲突/非 DB 损坏)
        与 OSError 同样 fail-closed 包装为 LifecycleError。
        """
        for l2, path in zip(self._l2, self.files):
            try:
                l2.snapshot_template(self.BASELINE)   # live→tpl(bbackup 页级/或拷贝)
            except (OSError, sqlite3.Error,
                    SnapshotConsistencyError) as exc:
                raise LifecycleError(
                    f"基线捕获失败({path}): {exc}") from exc
            if not l2.template_exists(self.BASELINE):
                raise LifecycleError(f"基线模板未建成({path})")
        self._established = True

    def begin_run(self) -> None:
        pass                                          # 还原在 end_run(输入内可见性)

    def end_run(self) -> None:
        if not self._established:
            raise LifecycleError("end_run 前未 establish_baseline(内部错误)")
        for hook in self.pre_restore_hooks:           # 1. 关连接(文件覆盖前)
            _fail(hook, "pre-restore 钩子(关连接)")
        if self.gc_before_restore:                    # 2. 兜底回收悬挂连接
            gc.collect()
        for l2, path in zip(self._l2, self.files):    # 3. 文件还原
            if not l2.template_exists(self.BASELINE):
                raise LifecycleError(
                    f"基线模板丢失({path})——隔离失效,fail-closed")
            try:
                l2.restore(self.BASELINE)
            except (OSError, sqlite3.Error,
                    SnapshotConsistencyError) as exc:
                raise LifecycleError(f"基线还原失败({path}): {exc}") from exc
        for hook in self.memory_hooks:                # 4. 内存态恢复
            _fail(hook, "memory-reset 钩子")


# ------------------------------------------------------------- 执行上下文

@dataclass(frozen=True)
class ExecutionResult:
    """一次输入执行的可观测产物。

    ``response`` 兼容层:现有 ``target(data)`` 返回值通常是 None,**不能**
    假设能拿到 HTTP 响应——None 表示"响应不可得"(非"空响应");未来
    靶侧可经钩子填充。
    """

    data: bytes = b""
    observations: tuple["StateObservation", ...] = ()   # 还原前的运行时观测
    lifecycle: str = "noop"
    isolation: str = ISOLATION_NONE
    response: Any = None


class ExecutionContext:
    """测试输入级执行器:生命周期 + 观测编排,显式状态隔离边界。

    用法(采集器分步形态,观测/还原在 trace 窗口外)::

        ctx.begin_run()                  # 失败 → LifecycleError(未执行)
        target(data)                     # 异常策略归调用方(旧语义不变)
        result = ctx.finish_run(data)    # 观测(还原前)→ 还原;失败 → LifecycleError

    一体式 ``run(fn, data)`` = begin → fn → finish,供单窗口调用方
    (off 模式/测试)。fail-closed:任一次 LifecycleError 后 ``poisoned``,
    后续执行一律拒绝(防污染态上继续实验)。
    """

    def __init__(self, lifecycle: StateLifecycle | None = None,
                 observer: "RuntimeObserver | None" = None):
        self.lifecycle: StateLifecycle = lifecycle or NoopLifecycle()
        self.observer = observer            # None = 无运行时观测(显式退化)
        self.runs = 0
        self.observer_failures = 0
        self._poisoned: str | None = None
        self._in_run = False
        self._baseline_done = False

    # ------------------------------------------------------------ 属性

    @property
    def name(self) -> str:
        return self.lifecycle.name

    @property
    def isolation(self) -> str:
        return self.lifecycle.isolation

    @property
    def poisoned(self) -> bool:
        return self._poisoned is not None

    def _ensure_alive(self) -> None:
        if self._poisoned is not None:
            raise LifecycleError(
                f"上下文已污染(fail-closed),拒绝后续执行: {self._poisoned}")

    # ------------------------------------------------------------ 分步 API

    def begin_run(self) -> None:
        """输入开始:惰性建基线(首输入前=应用干净态)+ lifecycle begin。"""
        self._ensure_alive()
        if self._in_run:
            raise RuntimeError("begin_run 未配对 finish_run")
        try:
            if not self._baseline_done:
                self.lifecycle.establish_baseline()
                self._baseline_done = True
            self.lifecycle.begin_run()
        except LifecycleError as exc:
            self._poisoned = str(exc)
            raise
        self._in_run = True

    def finish_run(self, data: bytes = b"") -> ExecutionResult:
        """输入结束:观测(还原前)→ lifecycle end(还原/复位)。

        观测器失败不 poison(证据缺失 ≠ 状态污染):计 observer_failures、
        observations 置空——调用方可据此显式退化到代理模式。
        """
        if not self._in_run:
            raise RuntimeError("finish_run 未配对 begin_run")
        self._in_run = False
        observations: tuple = ()
        if self.observer is not None:
            try:
                observations = tuple(self.observer.observe())
            except Exception:                          # noqa: BLE001
                self.observer_failures += 1            # 审计计数,不冒充不阻断
        try:
            self.lifecycle.end_run()
        except LifecycleError as exc:
            self._poisoned = str(exc)
            raise
        self.runs += 1
        return ExecutionResult(data=data, observations=observations,
                               lifecycle=self.lifecycle.name,
                               isolation=self.lifecycle.isolation)

    # ------------------------------------------------------------ 一体式

    def run(self, fn: Callable[[bytes], Any], data: bytes) -> ExecutionResult:
        """begin → fn(data) → finish;fn 的异常在还原后原样重抛。

        若 fn 抛异常且还原也失败,LifecycleError 优先上报(隔离失效比
        单输入错误更重要;原始异常挂在 __context__ 链上)。
        """
        self.begin_run()
        error: Exception | None = None
        try:
            fn(data)
        except Exception as exc:                       # noqa: BLE001
            error = exc
        result = self.finish_run(data)                 # 可能抛 LifecycleError
        if error is not None:
            raise error
        return result


# ------------------------------------------------------------- 靶模块解析

def lifecycle_from_module(module, spec: str = ISOLATION_NONE) -> StateLifecycle:
    """靶模块 → 生命周期后端(约定属性名,无靶名特判)。

    - noop:NoopLifecycle(任何靶可用,旧行为);
    - reset:模块 ``reset_state()``(既有约定名);
    - snapshot:模块 ``SF_STATE_FILES`` + 可选 ``SF_MEMORY_RESET``。
    约定缺失 → LifecycleError(配置错误要在首个输入前炸,不是静默 noop)。
    """
    if spec in ("noop", ISOLATION_NONE):             # CLI 词 = noop,常量 = none
        return NoopLifecycle()
    if spec == ISOLATION_RESET:
        hook = getattr(module, RESET_HOOK_ATTR, None)
        if not callable(hook):
            raise LifecycleError(
                f"--lifecycle reset 需要靶模块提供 {RESET_HOOK_ATTR}()")
        return ResetLifecycle([hook])
    if spec == ISOLATION_SNAPSHOT:
        files = [str(f) for f in (getattr(module, SNAPSHOT_FILES_ATTR, ()) or ())]
        mem = getattr(module, MEMORY_RESET_ATTR, None)
        memory_hooks = [mem] if callable(mem) else []
        if not files and not memory_hooks:
            raise LifecycleError(
                f"--lifecycle snapshot 需要靶模块提供 {SNAPSHOT_FILES_ATTR}"
                f" 或 {MEMORY_RESET_ATTR}")
        return SnapshotLifecycle(files=files, memory_hooks=memory_hooks)
    raise ValueError(f"未知 lifecycle 规格: {spec!r}")


def observer_from_module(module) -> "RuntimeObserver | None":
    """靶模块 → 运行时观测器;无 ``sf_state_observer`` → None(显式退化)。"""
    factory = getattr(module, OBSERVER_ATTR, None)
    if not callable(factory):
        return None
    return factory()


# ------------------------------------------------------------- 失败审计

def audit_lifecycle_failure(out_dir: str | Path, record: dict) -> None:
    """生命周期失败审计 JSONL(尽力而为,不反噬主流程)。

    与 epoch._llm_audit 同语义:审计落盘自身失败(目录不可写等)静默
    放弃——审计痕是尽力而为,不得反噬 fuzz 循环;但**生命周期失败本身
    绝不静默**:调用方必须跳过该输入(不计执行/覆盖/语料)。
    """
    try:
        path = Path(out_dir) / "lifecycle_failures.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), **record},
                                ensure_ascii=False) + "\n")
    except OSError:
        pass


# ------------------------------------------------------------- PR-04 逐请求观测

@dataclass(frozen=True)
class StepRecord:
    """M0-B 一步请求的完整记录(观测/响应/异常/耗时/绑定/轨迹窗口)。"""

    index: int
    action: str
    status: object = None                 # HTTP 状态码(或 None=异常/无响应)
    error: str | None = None              # 异常分类(不吞:记录后继续)
    before: tuple = ()                    # 请求前观测(duck-typed observations)
    after: tuple = ()                     # 请求后观测(4xx/异常同样执行)
    duration_ms: float = 0.0
    bindings: dict = field(default_factory=dict)   # 执行后绑定快照
    trace_span: tuple[int, int] = (0, 0)  # session.events 窗口[start,end)


@dataclass(frozen=True)
class InputRecord:
    """M0-B 一个输入(序列)的记录:生命周期每输入一次,观测每请求两次。"""

    steps: tuple[StepRecord, ...] = ()
    lifecycle_failures: int = 0
    observer_failures: int = 0


class RequestLevelHarness:
    """序列输入的逐请求观测编排(M0-B;任务书 PR-04 概念 API 落地)。

    纪律:
    - ``begin_input``/``finish_input`` 各一次——生命周期(含 DB 恢复)
      只发生在**输入边界**,序列内前序请求的状态对后续请求可见;
    - before/after 观测经 ``trace_session.untraced()`` 窗口(提供
      session 时)——观测帧不进覆盖计数(P0-B 纪律的 Trace 侧延伸);
    - 4xx/5xx/异常**同样执行 after 观测**并记录 error(失败请求仍可能
      产生副作用;异常不向调用方泄,记录后继续下一步);
    - 观测器异常:计 observer_failures + 空观测(证据缺失≠状态污染),
      不 crash;
    - execute_step(step) -> status 由调用方提供(适配任意请求面);
      步内 Trace 采集:提供 trace_session 时自动开 request 窗口并记
      trace_span(调用方事后 map_trace 判定目标块)。

    域归属:runtime(L2)——observer/bindings 全程鸭子类型,零 state 依赖。
    """

    def __init__(self, execution: "ExecutionContext",
                 observer=None, bindings_fn=None, extra_observe=None):
        self.execution = execution
        self.observer = observer
        self.bindings_fn = bindings_fn
        self.extra_observe = extra_observe      # step → 附加观测(如认证探测)

    def _observe(self, session, step=None, extra=None):
        if self.observer is None and extra is None:
            return ()
        out: list = []
        try:
            if session is not None:
                with session.untraced():
                    if self.observer is not None:
                        out.extend(self.observer.observe())
                    if extra is not None and step is not None:
                        out.extend(extra(step))
            else:
                if self.observer is not None:
                    out.extend(self.observer.observe())
                if extra is not None and step is not None:
                    out.extend(extra(step))
        except Exception:                          # noqa: BLE001
            self._observer_failures += 1
            return ()
        return tuple(out)

    _observer_failures = 0

    def run_input(self, steps, execute_step, *,
                  trace_session=None, action_of=None) -> InputRecord:
        """执行一个输入:begin →(before→execute→after)×N → finish。

        构造期 ``extra_observe(step)``:随步上下文的附加观测(如认证
        探测——须以**该步请求实际携带**的凭据为准,而非全局绑定状态);
        在 before/after 两个窗口各调用一次,与基础观测同窗口(同观测点)。
        """
        try:
            self.execution.begin_run()             # 输入边界(首输入建基线)
        except LifecycleError:
            return InputRecord(lifecycle_failures=1)

        records: list[StepRecord] = []
        for idx, step in enumerate(steps):
            before = self._observe(trace_session, step, self.extra_observe)
            t0 = time.monotonic()
            start_ev = len(trace_session.events) if trace_session else 0
            status, error = None, None
            try:
                if trace_session is not None:
                    with trace_session.request():
                        status = execute_step(step)
                else:
                    status = execute_step(step)
            except Exception as exc:               # noqa: BLE001
                error = f"{type(exc).__name__}:{exc}"
            end_ev = len(trace_session.events) if trace_session else 0
            duration_ms = (time.monotonic() - t0) * 1000
            after = self._observe(trace_session, step,
                                  self.extra_observe)  # 4xx/异常同样观测
            bindings = dict(self.bindings_fn()) if self.bindings_fn else {}
            records.append(StepRecord(
                index=idx,
                action=action_of(idx, step) if action_of
                else str(step.get("method", "")) + " " + str(step.get("path", step)),
                status=status, error=error, before=before, after=after,
                duration_ms=round(duration_ms, 3), bindings=bindings,
                trace_span=(start_ev, end_ev)))

        try:
            self.execution.finish_run()            # 输入边界(还原)
        except LifecycleError:
            return InputRecord(steps=tuple(records), lifecycle_failures=1,
                               observer_failures=self._observer_failures)
        return InputRecord(steps=tuple(records),
                           observer_failures=self._observer_failures)
