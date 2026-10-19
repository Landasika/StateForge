"""test_l2_wal.py —— PR-02:SQLite 基线捕获/恢复的 WAL 一致性与使用边界。

任务书必测八项的机器执行面:
1. WAL 已提交未 checkpoint 数据可捕获并恢复;
2. 捕获期间活动连接:一致快照或可审计 fail-closed;
3. 未提交事务不进快照;
4. 恢复后逐表逻辑状态与基线一致;
5. DELETE journal 与非 SQLite 文件回归;
6. 恢复遇未关闭(持锁)连接不静默继续;
7. fork-COW 路径(copy 模式)遇 WAL fail-closed;
8. 输入 A 的写不影响输入 B(同一基线),输入内多请求共享状态。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from stateforge.runtime.execution_context import (
    ExecutionContext,
    LifecycleError,
    SnapshotLifecycle,
)
from stateforge.runtime.snapshot.l2_sqlite import (
    L2Sqlite,
    SnapshotConsistencyError,
)


def _make_wal_db(path: Path, rows: list[int]) -> sqlite3.Connection:
    """建 WAL 库并**返回 keeper 连接**:最后一个连接关闭时 SQLite 会
    checkpoint 并删除 -wal,keeper 保证"已提交未 checkpoint 帧"状态存活,
    测试结束自行 close。"""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")   # 已提交帧留在 -wal
    conn.executescript("CREATE TABLE t(x INTEGER)")
    conn.commit()
    for x in rows:
        conn.execute("INSERT INTO t(x) VALUES(?)", (x,))
        conn.commit()
    return conn                                    # keeper:勿关,直到测试结束


def _make_wal_db_rows(path: Path) -> sqlite3.Connection:
    keeper = _make_wal_db(path, [1, 2])
    return keeper


def _rows(path: Path) -> list[int]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    out = [r[0] for r in conn.execute("SELECT x FROM t ORDER BY x")]
    conn.close()
    return out


def test_wal_committed_uncheckpointed_captured_and_restored(tmp_path):
    """必测 1:WAL 已提交未 checkpoint 帧 → 模板含该数据;破坏写后恢复回基线。"""
    db = tmp_path / "app.db"
    keeper = _make_wal_db(db, [1, 2, 3])
    try:
        assert Path(str(db) + "-wal").exists()    # 帧确在 WAL 中未落盘

        l2 = L2Sqlite(db, tpl_root=tmp_path / "tpl")
        l2.snapshot_template("baseline")
        assert _rows(l2._tpl("baseline")) == [1, 2, 3]

        conn = sqlite3.connect(db)                # 基线后的破坏写
        conn.execute("INSERT INTO t(x) VALUES(999)")
        conn.commit()
        conn.close()
        l2.restore("baseline")
        assert _rows(db) == [1, 2, 3]             # 破坏写消失,基线全量在
    finally:
        keeper.close()


def test_capture_does_not_delete_live_sidecars(tmp_path):
    """PR-02 硬规则:backup 捕获不删活跃数据库的 -wal/-shm。"""
    db = tmp_path / "app.db"
    keeper = _make_wal_db(db, [7])
    try:
        wal = Path(str(db) + "-wal")
        assert wal.exists()
        l2 = L2Sqlite(db, tpl_root=tmp_path / "tpl")
        l2.snapshot_template("baseline")
        assert wal.exists()                       # 未被删除(活跃连接可能要用)
    finally:
        keeper.close()


def test_capture_with_active_connection_consistent_or_fail(tmp_path):
    """必测 2:捕获期存在活动连接(WAL 空闲写者)→ 仍得一致快照。"""
    db = tmp_path / "app.db"
    keeper = _make_wal_db(db, [1])
    writer = sqlite3.connect(db)                  # 活跃连接(WAL,空闲)
    try:
        l2 = L2Sqlite(db, tpl_root=tmp_path / "tpl")
        l2.snapshot_template("baseline")
        assert _rows(l2._tpl("baseline")) == [1]  # 一致快照
    finally:
        writer.close()
        keeper.close()


def test_capture_with_exclusive_lock_delete_mode(tmp_path):
    """必测 2b(PR-02.1 收紧):DELETE+EXCLUSIVE 持锁者捕获 → **可审计
    fail-closed**。PR-02 版曾按"主文件=已提交态"接受拷贝回退;但提交中
    瞬间主文件可短暂不自洽(EXCLUSIVE 持有期),拷贝不能证明一致——
    probe 无法区分"锁死"与"损坏",一律 fail-closed(评审 PR-02.1:
    按 SQLite 头/错误分类收口,不静默退化普通拷贝)。空闲连接的
    一致快照路径由必测 2(WAL)覆盖。"""
    from stateforge.runtime.snapshot.l2_sqlite import SnapshotConsistencyError
    db = tmp_path / "app.db"
    conn = sqlite3.connect(db)
    conn.executescript("CREATE TABLE t(x INTEGER); INSERT INTO t VALUES(1)")
    conn.commit()
    conn.close()
    blocker = sqlite3.connect(db, timeout=0)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        l2 = L2Sqlite(db, tpl_root=tmp_path / "tpl")
        with pytest.raises(SnapshotConsistencyError, match="锁死"):
            l2.snapshot_template("baseline")
    finally:
        blocker.rollback()
        blocker.close()


def test_uncommitted_txn_excluded_from_snapshot(tmp_path):
    """必测 3:未提交事务的数据不进快照(backup 快照读语义)。"""
    db = tmp_path / "app.db"
    keeper = _make_wal_db(db, [1])
    writer = sqlite3.connect(db)
    writer.execute("INSERT INTO t(x) VALUES(2)")  # 未 commit
    try:
        l2 = L2Sqlite(db, tpl_root=tmp_path / "tpl")
        l2.snapshot_template("baseline")
        assert _rows(l2._tpl("baseline")) == [1]  # 只含已提交
    finally:
        writer.rollback()
        writer.close()
        keeper.close()


def test_restore_logical_equivalence_per_table(tmp_path):
    """必测 4:恢复后逐表(多表)逻辑状态与基线一致。"""
    db = tmp_path / "app.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE a(x INTEGER); CREATE TABLE b(y TEXT);"
        "INSERT INTO a VALUES(1),(2); INSERT INTO b VALUES('k');")
    conn.commit()
    conn.close()
    l2 = L2Sqlite(db, tpl_root=tmp_path / "tpl")
    l2.snapshot_template("baseline")

    conn = sqlite3.connect(db)                    # 基线后双向破坏
    conn.execute("DELETE FROM a WHERE x=1")
    conn.execute("INSERT INTO b VALUES('dirty')")
    conn.commit()
    conn.close()
    l2.restore("baseline")

    conn = sqlite3.connect(db)
    a = [r[0] for r in conn.execute("SELECT x FROM a ORDER BY x")]
    b = [r[0] for r in conn.execute("SELECT y FROM b ORDER BY y")]
    conn.close()
    assert (a, b) == ([1, 2], ["k"])


def test_delete_mode_and_plain_file_regressions(tmp_path):
    """必测 5:journal DELETE 模式与普通非 SQLite 文件回归。"""
    # DELETE 模式 DB:backup 路径同样工作
    db = tmp_path / "del.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.executescript("CREATE TABLE t(x INTEGER); INSERT INTO t VALUES(5)")
    conn.commit()
    conn.close()
    l2 = L2Sqlite(db, tpl_root=tmp_path / "tpl")
    l2.snapshot_template("b")
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO t VALUES(6)")
    conn.commit()
    conn.close()
    l2.restore("b")
    assert _rows(db) == [5]

    # 非 SQLite 文件:普通拷贝
    f = tmp_path / "state.json"
    f.write_text('{"k": 1}', encoding="utf-8")
    l2f = L2Sqlite(f, tpl_root=tmp_path / "tplf")
    l2f.snapshot_template("b")
    f.write_text("dirty", encoding="utf-8")
    l2f.restore("b")
    assert f.read_text(encoding="utf-8") == '{"k": 1}'


def test_restore_with_held_lock_fails_closed(tmp_path):
    """必测 6:恢复遇未关闭(持锁)连接 → OperationalError → 不静默继续。"""
    db = tmp_path / "app.db"
    conn = sqlite3.connect(db)
    conn.executescript("CREATE TABLE t(x INTEGER)")
    conn.commit()
    conn.close()
    l2 = L2Sqlite(db, tpl_root=tmp_path / "tpl")
    l2.snapshot_template("b")

    blocker = sqlite3.connect(db, timeout=0)
    blocker.execute("BEGIN EXCLUSIVE")            # 模拟靶侧未关闭的持锁连接
    try:
        with pytest.raises(SnapshotConsistencyError, match="持锁"):
            l2.restore("b")                       # fail-closed,原因可审计
    finally:
        blocker.rollback()
        blocker.close()


def test_fork_cow_copy_mode_wal_fails_closed(tmp_path):
    """必测 7:fork-COW(copy 模式)遇 WAL → SnapshotConsistencyError(fail-closed),
    不静默降级;DELETE 模式保持位级行为。"""
    wdb = tmp_path / "wal.db"
    _make_wal_db(wdb, [1])
    l2w = L2Sqlite(wdb, tpl_root=tmp_path / "tplw",
                   capture=L2Sqlite.CAPTURE_COPY)
    with pytest.raises(SnapshotConsistencyError, match="WAL"):
        l2w.snapshot_template("root")

    ddb = tmp_path / "del.db"
    conn = sqlite3.connect(ddb)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.executescript("CREATE TABLE t(x INTEGER); INSERT INTO t VALUES(1)")
    conn.commit()
    conn.close()
    l2d = L2Sqlite(ddb, tpl_root=tmp_path / "tpld",
                   capture=L2Sqlite.CAPTURE_COPY)
    tpl = l2d.snapshot_template("root")           # 位级拷贝(既有语义)
    assert tpl.read_bytes() == ddb.read_bytes()


def test_input_isolation_and_intra_input_sharing_wal(tmp_path):
    """必测 8:WAL 靶上——输入 A 的写不泄漏进输入 B;输入内两"请求"共享状态。"""
    db = tmp_path / "app.db"
    keeper = _make_wal_db(db, [])

    class WalApp:
        def __init__(self, p: Path):
            self.p = p
            self.calls: list[tuple[str, int]] = []

        def target(self, data: bytes) -> None:
            conn = sqlite3.connect(self.p)        # 请求 1:写
            conn.execute("INSERT INTO t(x) VALUES(?)", (len(data),))
            conn.commit()
            conn.close()
            self.calls.append(("w", self._count()))
            self.calls.append(("r", self._count()))   # 请求 2:读(见请求 1)

        def _count(self) -> int:
            conn = sqlite3.connect(self.p)
            n = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
            conn.close()
            return n

    app = WalApp(db)
    ctx = ExecutionContext(SnapshotLifecycle(files=[db]))
    ctx.run(app.target, b"a")
    ctx.run(app.target, b"bb")
    # 输入内:读见写(共享);两次输入中间态一致(= 基线+本输入 1 行)
    assert app.calls == [("w", 1), ("r", 1), ("w", 1), ("r", 1)]
    # 输入后:回基线(0 行)——A 的写不影响 B
    assert app._count() == 0
    keeper.close()


# --------------------------------------------------- PR-02.1:防御补丁

def test_corrupt_sqlite_header_fails_closed_not_plain_copy(tmp_path):
    """SQLite 魔数 + 损坏内容 → fail-closed;不静默退化成普通拷贝。"""
    from stateforge.runtime.snapshot.l2_sqlite import SnapshotConsistencyError
    db = tmp_path / "corrupt.db"
    db.write_bytes(b"SQLite format 3\x00" + b"\x00garbage" * 64)
    l2 = L2Sqlite(db, tpl_root=tmp_path / "tpl")
    with pytest.raises(SnapshotConsistencyError, match="损坏"):
        l2.snapshot_template("baseline")
    # 非 SQLite 垃圾(无魔数)仍走普通拷贝(既有语义)
    plain = tmp_path / "plain.bin"
    plain.write_bytes(b"\x00garbage" * 64)
    l2p = L2Sqlite(plain, tpl_root=tmp_path / "tplp")
    tpl = l2p.snapshot_template("baseline")
    assert tpl.read_bytes() == plain.read_bytes()


def test_corrupt_template_fails_closed_on_restore(tmp_path):
    from stateforge.runtime.snapshot.l2_sqlite import SnapshotConsistencyError
    db = tmp_path / "ok.db"
    conn = sqlite3.connect(db)
    conn.executescript("CREATE TABLE t(x INTEGER)")
    conn.commit()
    conn.close()
    l2 = L2Sqlite(db, tpl_root=tmp_path / "tpl")
    l2.snapshot_template("baseline")
    l2._tpl("baseline").write_bytes(          # 模板被外部破坏成"魔数+垃圾"
        b"SQLite format 3\x00" + b"\x00bad" * 32)
    with pytest.raises(SnapshotConsistencyError, match="模板"):
        l2.restore("baseline")


def test_active_reader_during_restore_is_audited(tmp_path):
    """活跃**读者**(WAL 打开读事务):IMMEDIATE 探针下恢复仍可进行
    (WAL 读者不阻塞写者预约);垃圾/持锁写者路径已由必测 6 覆盖——
    本例固化读者边界可审计(不 crash、结果正确)。"""
    db = tmp_path / "r.db"
    _make_wal_db_rows(db)
    l2 = L2Sqlite(db, tpl_root=tmp_path / "tpl")
    l2.snapshot_template("baseline")
    conn = sqlite3.connect(db)                # 读者:开一个读事务
    conn.execute("BEGIN")
    conn.execute("SELECT COUNT(*) FROM t").fetchone()
    try:
        conn2 = sqlite3.connect(db)           # 基线后的破坏写
        conn2.execute("INSERT INTO t(x) VALUES(999)")
        conn2.commit()
        conn2.close()
        l2.restore("baseline")                # 读者在场:WAL 下预约可获取
        assert _rows(db) == [1, 2]
    finally:
        conn.rollback()
        conn.close()
