"""l2_sqlite.py —— SF2-T2:L2 文件 DB 模板恢复(ADR-0004:位级模板拷贝 + journal 清理)。

问题:fork COW 只隔离内存,叶对文件的写会落盘污染下一 epoch。
正确性论证:检查点父进程的 SQLite 页缓存对应冻结时刻;模板 = 冻结时刻
文件的位拷贝 → 还原后磁盘与父进程缓存精确一致(无脏页检测问题);
被 SIGKILL 的叶可能留热 journal,删除即可(模板拷贝等价回滚)。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


class L2Sqlite:
    def __init__(self, live_path: str, tpl_root: str | None = None):
        self.live = Path(live_path)
        root = Path(tpl_root) if tpl_root else self.live.parent / "tpl"
        self.root = root

    def _tpl(self, name: str) -> Path:
        return self.root / name / "db.tpl"

    def snapshot_template(self, name: str = "root") -> Path:
        """冻结时:copy(live → tpl/<ckpt>/db.tpl)。"""
        dst = self._tpl(name)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if self.live.exists():
            shutil.copyfile(self.live, dst)
            for suffix in ("-journal", "-wal", "-shm"):
                side = Path(str(self.live) + suffix)
                if side.exists():
                    side.unlink()
        return dst

    def restore(self, name: str = "root") -> None:
        """每叶 fork 前:copy(tpl → live);unlink 热_journal/-wal/-shm。"""
        src = self._tpl(name)
        if not src.exists():
            return                          # 纯内存目标(None db)无操作
        self.live.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, self.live)
        for suffix in ("-journal", "-wal", "-shm"):
            side = Path(str(self.live) + suffix)
            if side.exists():
                side.unlink()

    # 引擎按当前检查点名还原;engine 只调无参版本 → 由 supervisor 维护"当前名"


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
