"""l2_sqlite.py —— L2 文件 DB 模板捕获/恢复(PR-02 重设计,ADR-0004 演进)。

两种捕获模式(消费方按一致性需求显式选择):

- ``capture="backup"``(缺省,**输入级 SnapshotLifecycle 消费**):
  ``sqlite3.Connection.backup()`` 页级一致快照——WAL 下自动含已提交
  未 checkpoint 帧;捕获**不触碰** live 的 ``-wal/-shm``(活跃连接可能
  正在使用);并发写者由 backup 内部页计数重试保证一致性。恢复 =
  删 sidecar(由生命周期前置条件保证无活跃连接)→ ``backup(tpl→live)``
  (``timeout=0``:任何残余锁立即 OperationalError → 上层 fail-closed)。

- ``capture="copy"``(**fork-COW 引擎/supervisor 专用**,位级语义):
  保留既有"文件拷贝 + sidecar 清理"(ADR-0004 的正确性论证依赖
  模板字节 == 冻结时刻文件字节 == 父进程页缓存);backup 会重排页,
  与冻结父进程的页缓存不再逐字节一致——故 WAL 模式下** fail-closed**
  (引擎路径暂不支持 WAL,显式报错,不静默降级)。journal DELETE 模式
  行为与历史版本逐字节等价。

非 SQLite 文件(SF_STATE_FILES 中的普通文件):两模式都退化为普通拷贝
(无 sidecar 语义);探测失败(DatabaseError)即走该路径。

历史契约兼容:模板布局 ``tpl/<name>/db.tpl``、``template_exists``、
纯内存目标(live 不存在→无模板/restore 静默返回)语义不变。
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")


class SnapshotConsistencyError(RuntimeError):
    """捕获/恢复一致性无法保证(fork-COW 遇 WAL、活跃锁等)——fail-closed。"""


class L2Sqlite:
    CAPTURE_BACKUP = "backup"   # 逻辑一致快照(WAL 安全;输入级生命周期)
    CAPTURE_COPY = "copy"       # 位级拷贝(fork-COW;WAL 下 fail-closed)
    _MODES = (CAPTURE_BACKUP, CAPTURE_COPY)

    def __init__(self, live_path: str, tpl_root: str | None = None,
                 capture: str = CAPTURE_BACKUP):
        self.live = Path(live_path)
        root = Path(tpl_root) if tpl_root else self.live.parent / "tpl"
        self.root = root
        if capture not in self._MODES:
            raise ValueError(f"capture 必须是 {self._MODES} 之一: {capture!r}")
        self.capture = capture

    # ------------------------------------------------------------- 模板面

    def _tpl(self, name: str) -> Path:
        return self.root / name / "db.tpl"

    def template_exists(self, name: str = "root") -> bool:
        """模板在场判定(纯内存目标语义由缺模板表达,不变)。"""
        return self._tpl(name).exists()

    # ------------------------------------------------------------- 探测

    def _probe_conn(self) -> sqlite3.Connection | None:
        """live 以只读模式可作 SQLite 打开 → 连接;非 DB/不可开 → None。"""
        if not self.live.exists():
            return None
        try:
            conn = sqlite3.connect(f"file:{self.live}?mode=ro", uri=True,
                                   timeout=0)
            conn.execute("PRAGMA schema_version").fetchone()
            return conn
        except sqlite3.Error:
            return None

    def _journal_mode_is_wal(self) -> bool:
        conn = self._probe_conn()
        if conn is None:
            return False
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            return str(mode).lower() == "wal"
        except sqlite3.Error:
            return False
        finally:
            conn.close()

    # ------------------------------------------------------------- 捕获

    def snapshot_template(self, name: str = "root") -> Path:
        """冻结时刻 live → 模板。按 capture 模式分派(见模块 docstring)。"""
        dst = self._tpl(name)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not self.live.exists():
            return dst                       # 纯内存目标:无模板(现状语义)

        if self.capture == self.CAPTURE_COPY:
            if self._journal_mode_is_wal():
                raise SnapshotConsistencyError(
                    f"copy 模式(fork-COW 位级冻结)不支持 WAL 目标: "
                    f"{self.live}(backup 会重排页,破坏'模板字节==冻结进程"
                    f"页缓存'的 ADR-0004 前提;引擎路径暂不支持 WAL)")
            shutil.copyfile(self.live, dst)
            self._unlink_sidecars()          # DELETE 模式:既有清理语义
            return dst

        src = self._probe_conn()
        if src is None:                      # 非 SQLite 文件:普通拷贝
            shutil.copyfile(self.live, dst)
            return dst
        try:
            dst_conn = sqlite3.connect(dst, timeout=0)
            try:
                src.backup(dst_conn)         # 页级一致(含 WAL 已提交帧)
            finally:
                dst_conn.close()
        finally:
            src.close()
        # backup 捕获不删除 live sidecar(活跃连接可能正在使用)——
        # 与 copy 模式的关键差异(PR-02:不删活跃数据库的 -wal/-shm)。
        return dst

    # ------------------------------------------------------------- 恢复

    def restore(self, name: str = "root") -> None:
        """模板 → live。缺模板 → 静默返回(纯内存目标,现状语义)。

        copy 模式:位级拷贝 + sidecar 清理(fork-COW 冻结一致性要求
        模板字节 == live 字节,不用 backup——backup 可能重排页)。
        backup 模式:经 SQLite 页机制恢复,WAL 语义自洽。
        """
        src_path = self._tpl(name)
        if not src_path.exists():
            return

        if self.capture == self.CAPTURE_COPY:
            self._unlink_sidecars()
            self.live.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src_path, self.live)
            return

        tpl_probe = self._probe_template_conn(src_path)
        if tpl_probe is None:                # 非 SQLite 模板:普通拷贝路径
            self._unlink_sidecars()
            self.live.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src_path, self.live)
            return
        tpl_probe.close()

        # SQLite 路径(backup 模式)恢复序:
        #   connect(live, timeout=0) → BEGIN IMMEDIATE 探针 → backup(tpl→live)
        #   → best-effort checkpoint → close。
        # 不主动 unlink live sidecar(PR-02:WAL 连接在场时删除即破坏,
        # "disk I/O error";垃圾/陈旧 sidecar 由 SQLite 自身生命周期清理
        # ——最后连接干净关闭时自动删除,已实证)。持锁探针:backup 对
        # 持续持锁目标会无限重试(pysqlite 语义),先以 BEGIN IMMEDIATE
        # (timeout=0)立即失败 → SnapshotConsistencyError(fail-closed,
        # 可审计);成功不推断"无其他连接"(只证明此刻无写者持锁)。
        self.live.parent.mkdir(parents=True, exist_ok=True)
        src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True, timeout=0)
        try:
            dst = sqlite3.connect(self.live, timeout=0)
            try:
                try:
                    dst.execute("BEGIN IMMEDIATE")
                    dst.execute("ROLLBACK")
                except sqlite3.OperationalError as exc:
                    raise SnapshotConsistencyError(
                        f"恢复目标被持锁连接占用(靶侧连接未关闭?): "
                        f"{self.live}: {exc}") from exc
                src.backup(dst)
                if self._journal_mode(dst) == "wal":
                    try:                      # 非致命:有其他读者时失败可接受
                        dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    except sqlite3.Error:
                        pass                   # 数据已在 WAL,一致性不受损
            finally:
                dst.close()
        finally:
            src.close()

    # ------------------------------------------------------------- 内部

    def _journal_mode(self, conn: sqlite3.Connection) -> str:
        try:
            return str(conn.execute("PRAGMA journal_mode")
                       .fetchone()[0]).lower()
        except sqlite3.Error:
            return ""

    def _probe_template_conn(self, tpl: Path) -> sqlite3.Connection | None:
        try:
            conn = sqlite3.connect(f"file:{tpl}?mode=ro", uri=True, timeout=0)
            conn.execute("PRAGMA schema_version").fetchone()
            return conn
        except sqlite3.Error:
            return None

    def _unlink_sidecars(self) -> None:
        for suffix in _SIDECAR_SUFFIXES:
            side = Path(str(self.live) + suffix)
            if side.exists():
                side.unlink()


class L2Handle:
    """名字绑定的 L2 视图:engine 拿到的 restore() 永远还原本检查点的模板。"""

    def __init__(self, l2: L2Sqlite, name: str):
        self.l2, self.name = l2, name

    def restore(self) -> None:
        self.l2.restore(self.name)

    def snapshot_template(self, child_name: str) -> Path:
        return self.l2.snapshot_template(child_name)

    def child(self, name: str) -> "L2Handle":
        return L2Handle(self.l2, name)
