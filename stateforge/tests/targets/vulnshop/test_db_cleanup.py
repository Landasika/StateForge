"""vulnshop 靶临时 DB 清理(P5-T3 修复轮 1)——双机制钉死。

生产路径:entry_driver 子进程经 atheris.Fuzz() 由 libFuzzer _Exit 终止
(entry_driver.py:4/:188,recorder.py:3——产物靠逐行 flush 抗同一终止),
finally/atexit 被绕过。故靶模块的两机制分工:

- **死 pid 机会清扫**(实际回收 epoch 残留):模块载入时删名字里 pid
  已死的 vulnshop_<pid>_<mod>.db——覆盖 libFuzzer _Exit 与 kill -9;
- **atexit**(只覆盖干净退出,如测试进程):立即删自己的 DB。

本文件以 os._exit 子进程模拟 _Exit(同径绕过 atexit),钉:残留被下一
载入清扫、存活 pid 的 DB 保留(清扫只删死主)。
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

FUZZ_TARGET = (Path(__file__).resolve().parents[3] / "targets" / "vulnshop"
               / "fuzz_target.py")

# 子进程:真加载靶模块(flask 栈、create_app 建真 DB),断言 DB 在场,
# 然后 os._exit(0)——与 libFuzzer _Exit 同径,atexit/finally 均被绕过。
_CHILD = (
    "import importlib.util, os, sys\n"
    "spec = importlib.util.spec_from_file_location('sf_dbprobe_child', %r)\n"
    "mod = importlib.util.module_from_spec(spec)\n"
    "sys.modules[spec.name] = mod\n"
    "spec.loader.exec_module(mod)\n"
    "assert mod._DB.exists(), 'child DB not created'\n"
    "os._exit(0)\n"
) % str(FUZZ_TARGET)


def _fresh_load(tag: str):
    """父进程内新 spec 名重载靶模块(重执行模块代码 → 清扫到期)。"""
    spec = importlib.util.spec_from_file_location(f"sf_dbprobe_{tag}",
                                                  FUZZ_TARGET)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_dead_pid_db_swept_live_pid_kept():
    """os._exit 子进程(模拟 _Exit)的 DB 残留 → 下一载入清扫删除;
    存活 pid(本测试进程)的 DB 原样保留——清扫只删死主。"""
    subprocess.run([sys.executable, "-c", _CHILD], check=True, timeout=300)
    leftover = list(Path(tempfile.gettempdir()).glob(
        "vulnshop_*_sf_dbprobe_child.db"))
    assert len(leftover) == 1, "os._exit 应绕过 atexit 使 DB 残留"
    dead_pid = int(leftover[0].name.split("_")[1])
    try:
        os.kill(dead_pid, 0)
        raise AssertionError("子进程 pid 仍存活(subprocess.run 未 reap?)")
    except ProcessLookupError:
        pass                                   # 确认死主,清扫必判删

    live = (Path(tempfile.gettempdir())
            / f"vulnshop_{os.getpid()}_sf_dbprobe_live.db")
    live.write_bytes(b"")
    try:
        _fresh_load("parent")                  # 模块载入 → 死 pid 清扫
        assert not leftover[0].exists(), "死 pid DB 应被清扫"
        assert live.exists(), "存活 pid 的 DB 不得被清扫"
    finally:
        live.unlink(missing_ok=True)           # 测试自清(名含本进程 pid)
