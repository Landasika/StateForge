"""P0-A 执行上下文验收测试 —— 阶段 1 任务书五条验收条件的机器执行面。

验收条件(任务书原文):
1. 同一序列从基线执行两次,产生相同的可观测状态结果;
2. 不同测试序列的执行顺序不改变各自结果;
3. 一个序列内前序请求写入的状态,对后续请求可见;
4. NoopLifecycle 下原有行为保持不变(输入间状态延续,钉扎语义);
5. reset/snapshot 失败时不能悄悄继续执行(fail-closed + 审计)。

外加:WAL/SHM 副本文件清理、文件还原前关连接(顺序)、惰性基线捕获、
靶模块钩子解析、审计 JSONL。
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

from stateforge.runtime.execution_context import (
    ExecutionContext,
    LifecycleError,
    NoopLifecycle,
    ResetLifecycle,
    SnapshotLifecycle,
    audit_lifecycle_failure,
    lifecycle_from_module,
    observer_from_module,
)
from stateforge.state.runtime_observer import (
    Evidence,
    SCOPE_GLOBAL,
    SCOPE_RESOURCE,
    SCOPE_USER,
    StateKey,
    StateObservation,
)


# --------------------------------------------------------------- 夹具应用

class DbApp:
    """文件 SQLite 迷你靶:每输入两次"请求"(写+读),输入内可见性载体。"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        conn = sqlite3.connect(db_path)
        conn.executescript("CREATE TABLE t(x INTEGER)")
        conn.commit()
        conn.close()
        self.calls: list[tuple[str, int]] = []      # (请求, 可见行数)

    def _count(self) -> int:
        conn = sqlite3.connect(self.db_path)
        n = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        conn.close()
        return n

    def target(self, data: bytes) -> None:
        """一个输入 = 两条请求:请求1 写入,请求2 读(须见请求1 的写)。"""
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO t(x) VALUES(?)", (len(data),))
        conn.commit()
        conn.close()
        self.calls.append(("write", self._count()))    # 写后即读 = 输入内可见性
        self.calls.append(("read", self._count()))


class MemoryApp:
    """内存态靶(reset 生命周期):模块级 dict 即持久状态。"""

    def __init__(self):
        self.store: dict[str, int] = {}
        self.snapshots: list[dict] = []
        self.reset_calls = 0

    def target(self, data: bytes) -> None:
        self.store[repr(data)] = self.store.get(repr(data), 0) + 1
        self.snapshots.append(dict(self.store))

    def reset(self) -> None:
        self.reset_calls += 1
        self.store.clear()


# --------------------------------------------------- 验收 4:Noop 行为不变

def test_noop_keeps_cross_input_state_persistence():
    """Noop = 旧行为:输入间状态延续(钉扎语义),isolation 显式 'none'。"""
    app = MemoryApp()
    ctx = ExecutionContext(NoopLifecycle())
    assert ctx.isolation == "none" and ctx.name == "noop"
    ctx.run(app.target, b"a")
    ctx.run(app.target, b"b")
    ctx.run(app.target, b"a")
    assert app.store == {"b'a'": 2, "b'b'": 1}     # 状态跨输入延续
    assert ctx.runs == 3 and not ctx.poisoned


def test_noop_target_exception_propagates_same_as_before():
    """Noop 下 fn 异常照旧穿透调用方(旧 handle 的 except 语义不变)。"""
    ctx = ExecutionContext()

    def boom(_data: bytes) -> None:
        raise ValueError("fuzz input error")

    with pytest.raises(ValueError):
        ctx.run(boom, b"x")
    assert ctx.runs == 1                            # 还原(finish)已完整执行


# --------------------------------------------------- 验收 1/3:快照确定性

def test_snapshot_same_sequence_twice_same_observable(tmp_path):
    """验收 1:同一序列从基线执行两次,可观测结果相同(终态都回基线)。"""
    app = DbApp(tmp_path / "app.db")
    ctx = ExecutionContext(SnapshotLifecycle(files=[app.db_path]))

    r1 = ctx.run(app.target, b"seq-data")
    mid1 = list(app.calls)
    r2 = ctx.run(app.target, b"seq-data")
    mid2 = list(app.calls[len(mid1):])
    # 输入内:请求2 见到请求1 的写(验收 3);两次输入的可观测中间态一致
    assert mid1 == mid2 and mid1[0] == ("write", 1) and mid1[1] == ("read", 1)
    # 输入后:DB 回基线(0 行)——两次皆然
    assert app._count() == 0
    assert r1.isolation == "snapshot" and r2.isolation == "snapshot"
    assert ctx.runs == 2


def test_snapshot_intra_input_visibility(tmp_path):
    """验收 3:输入内前序请求写入对后续请求可见(隔离在输入边界)。"""
    app = DbApp(tmp_path / "app.db")
    ctx = ExecutionContext(SnapshotLifecycle(files=[app.db_path]))
    ctx.run(app.target, b"x")
    ctx.run(app.target, b"yy")                      # 第二输入:仍只见自己这轮 1 行
    assert app.calls[-1] == ("read", 1)             # 上一输入的行已随还原消失


def test_snapshot_order_independence(tmp_path):
    """验收 2:不同输入的执行顺序不改变各自结果。"""
    app = DbApp(tmp_path / "app.db")
    ctx = ExecutionContext(SnapshotLifecycle(files=[app.db_path]))
    for data in (b"a", b"bb", b"ccc"):
        ctx.run(app.target, data)
        assert app._count() == 0                    # 每输入后回基线 → 顺序无关
    assert all(c == ("write", 1) or c == ("read", 1) for c in app.calls)


def test_snapshot_baseline_captured_at_first_input_not_construction(tmp_path):
    """惰性基线:构造后、首输入前的准备写不入基线(首输入时刻=干净态)。"""
    app = DbApp(tmp_path / "app.db")
    ctx = ExecutionContext(SnapshotLifecycle(files=[app.db_path]))
    seed_conn = sqlite3.connect(app.db_path)        # 构造后补一行(模拟晚 init)
    seed_conn.execute("INSERT INTO t(x) VALUES(42)")
    seed_conn.commit()
    seed_conn.close()
    ctx.run(app.target, b"z")                       # 首输入才建基线(含 42)
    assert app._count() == 1                        # 还原到"含 42"的基线


def test_snapshot_cleans_wal_shm_sidecars(tmp_path):
    """L2 契约:还原时清理 -journal/-wal/-shm 副本(WAL 残留不复活)。"""
    app = DbApp(tmp_path / "app.db")
    for suffix in ("-wal", "-shm"):
        Path(str(app.db_path) + suffix).write_bytes(b"stale")
    ctx = ExecutionContext(SnapshotLifecycle(files=[app.db_path]))
    ctx.run(app.target, b"x")
    for suffix in ("-wal", "-shm"):
        assert not Path(str(app.db_path) + suffix).exists()


def test_snapshot_closes_connections_before_restore(tmp_path):
    """文件还原前先走 pre_restore 钩子(关连接),顺序可观测。"""
    app = DbApp(tmp_path / "app.db")
    order: list[str] = []
    conn = sqlite3.connect(app.db_path)             # 靶持有的悬挂连接
    try:
        ctx = ExecutionContext(SnapshotLifecycle(
            files=[app.db_path],
            pre_restore_hooks=[lambda: (order.append("close"), conn.close())]))
        ctx.run(app.target, b"x")
        assert order == ["close"]                   # 还原前关连接(未被 gc 兜底)
        assert app._count() == 0                    # 还原成功完成
    finally:
        conn.close()                                # 幂等:重复 close 无害


def test_snapshot_memory_hooks_reset_in_memory_state(tmp_path):
    """内存钩子在每输入后恢复到 establish 时刻(符号绑定表清空)。"""
    bindings: dict[str, str] = {}
    ctx = ExecutionContext(SnapshotLifecycle(files=[tmp_path / "a.db"],
                                             memory_hooks=[bindings.clear]))
    (tmp_path / "a.db").write_bytes(b"")            # 基线文件(空文件亦可)
    ctx.run(lambda d: bindings.update(token="t"), b"x")
    assert bindings == {}                           # 输入后内存态复位
    assert ctx.runs == 1


# --------------------------------------------------- 验收 5:fail-closed

def test_snapshot_restore_failure_fail_closed(tmp_path):
    """基线模板丢失 → LifecycleError,不静默;上下文 poisoned 拒绝后续。"""
    app = DbApp(tmp_path / "app.db")
    life = SnapshotLifecycle(files=[app.db_path])
    ctx = ExecutionContext(life)
    ctx.run(app.target, b"first")                   # 正常一轮(建基线+还原)
    shutil.rmtree(life._l2[0].root)                 # 模板被外部破坏

    with pytest.raises(LifecycleError, match="基线模板丢失"):
        ctx.run(app.target, b"poison")
    assert ctx.poisoned
    with pytest.raises(LifecycleError, match="已污染"):   # 后续一律拒绝
        ctx.run(app.target, b"next")
    assert ctx.runs == 1                            # 污染输入未计入


def test_reset_hook_failure_fail_closed_before_execution():
    """reset 钩子失败 → LifecycleError 且 target 不执行(不带脏态跑)。"""
    ran: list[bytes] = []

    def bad_reset() -> None:
        raise RuntimeError("reset exploded")

    ctx = ExecutionContext(ResetLifecycle([bad_reset]))
    with pytest.raises(LifecycleError, match="reset 钩子"):
        ctx.run(lambda d: ran.append(d), b"x")
    assert ran == [] and ctx.poisoned


def test_reset_lifecycle_isolates_inputs():
    """reset:每输入前拉回基线(验收 1/2 的内存态形态)。"""
    app = MemoryApp()
    ctx = ExecutionContext(ResetLifecycle([app.reset]))
    ctx.run(app.target, b"a")
    ctx.run(app.target, b"a")
    assert app.snapshots == [{"b'a'": 1}, {"b'a'": 1}]   # 两输入互不可见
    assert app.reset_calls == 2


# ------------------------------------------------------------- 观测编排

def test_observer_runs_before_restore(tmp_path):
    """观测时点 = 还原前:能读到该输入执行结束时的真实终态。"""
    app = DbApp(tmp_path / "app.db")

    class RowObserver:
        name = "row-count"

        def observe(self):
            return [StateObservation(
                key=StateKey("Table.t.rows", "*", SCOPE_GLOBAL),
                value=app._count() == 1,
                evidence=Evidence.RUNTIME_VERIFIED, source="db:t")]

    ctx = ExecutionContext(SnapshotLifecycle(files=[app.db_path]),
                           RowObserver())
    r = ctx.run(app.target, b"x")
    assert [o.key.predicate for o in r.observations] == ["Table.t.rows"]
    assert r.observations[0].value is True           # 执行后 1 行(还原前读)
    assert app._count() == 0                         # 还原照常发生
    assert r.lifecycle == "snapshot"


def test_observer_failure_does_not_poison_but_audits(tmp_path):
    """观测器失败 = 证据缺失 ≠ 状态污染:不 poison、计数、还原照常。"""
    app = DbApp(tmp_path / "app.db")

    class BrokenObserver:
        name = "broken"

        def observe(self):
            raise sqlite3.OperationalError("db locked")

    ctx = ExecutionContext(SnapshotLifecycle(files=[app.db_path]),
                           BrokenObserver())
    r = ctx.run(app.target, b"x")
    assert r.observations == () and ctx.observer_failures == 1
    assert not ctx.poisoned and app._count() == 0


# ------------------------------------------------------------- 模块解析

class _FakeTargetModule:
    reset_state_calls: list[int] = []

    def reset_state(self) -> None:
        self.reset_state_calls.append(1)

    SF_STATE_FILES: list[str] = []

    def SF_MEMORY_RESET(self) -> None:  # pragma: no cover - 形状占位
        pass


def test_lifecycle_from_module_dispatch():
    mod = _FakeTargetModule()
    assert isinstance(lifecycle_from_module(mod, "noop"), NoopLifecycle)

    reset = lifecycle_from_module(mod, "reset")
    assert isinstance(reset, ResetLifecycle)
    reset.begin_run()                                # 分派到模块 reset_state
    assert mod.reset_state_calls == [1]

    mod.SF_STATE_FILES = ["/tmp/x.db"]
    snap = lifecycle_from_module(mod, "snapshot")
    assert isinstance(snap, SnapshotLifecycle)
    assert [str(p) for p in snap.files] == ["/tmp/x.db"]


def test_lifecycle_from_module_missing_hooks_raise():
    """约定缺失 = 配置错误:首个输入前显式炸,不静默退化 noop。"""
    bare = type("Bare", (), {})()
    with pytest.raises(LifecycleError, match="reset_state"):
        lifecycle_from_module(bare, "reset")
    with pytest.raises(LifecycleError, match="SF_STATE_FILES"):
        lifecycle_from_module(bare, "snapshot")
    with pytest.raises(ValueError):
        lifecycle_from_module(bare, "bogus")


def test_observer_from_module_absent_returns_none():
    bare = type("Bare", (), {})()
    assert observer_from_module(bare) is None


# ------------------------------------------------------------- 审计

def test_audit_lifecycle_failure_writes_jsonl(tmp_path):
    audit_lifecycle_failure(tmp_path, {"group": "g", "epoch": 2,
                                       "error": "基线模板丢失"})
    audit_lifecycle_failure(tmp_path, {"group": "g", "epoch": 3,
                                       "error": "reset 钩子失败(x)"})
    lines = (tmp_path / "lifecycle_failures.jsonl").read_text(
        encoding="utf-8").splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["group"] == "g" and rec["epoch"] == 2 and "ts" in rec


def test_audit_lifecycle_failure_swallows_os_error(tmp_path):
    """审计落盘自身失败静默放弃(尽力而为,不反噬 fuzz 循环)。"""
    audit_lifecycle_failure(tmp_path / "no" / "such" / "dir", {"error": "x"})


# ------------------------------------------------------------- 契约细节

def test_run_re_raises_target_error_after_restore(tmp_path):
    """fn 异常在还原后重抛;还原失败时 LifecycleError 优先(fn 错挂链上)。"""
    app = DbApp(tmp_path / "app.db")
    life = SnapshotLifecycle(files=[app.db_path])
    ctx = ExecutionContext(life)
    ctx.run(app.target, b"warm")                     # 建基线

    def writes_then_raises(data: bytes) -> None:
        app.target(data)
        raise KeyError("mid-input crash")

    with pytest.raises(KeyError):
        ctx.run(writes_then_raises, b"x")            # 还原照常(fn 错后重抛)
    assert app._count() == 0 and ctx.runs == 2 and not ctx.poisoned

    # 还原也失败的场合:LifecycleError 优先于 fn 异常上报
    shutil.rmtree(life._l2[0].root)

    with pytest.raises(LifecycleError):
        ctx.run(writes_then_raises, b"y")
    assert ctx.poisoned


def test_begin_finish_pairing_guards(tmp_path):
    ctx = ExecutionContext()
    with pytest.raises(RuntimeError):
        ctx.finish_run()                             # 未 begin
    ctx.begin_run()
    with pytest.raises(RuntimeError):
        ctx.begin_run()                              # 重复 begin
    ctx.finish_run()
