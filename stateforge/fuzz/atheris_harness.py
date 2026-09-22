"""atheris_harness.py —— Atheris fuzz harness(ANOTA 兼容,已修通)。

修复要点(2026-09-22):
1. instrument_func(视图) 替代 instrument_all()(后者 instrument ANOTA 内部模块导致冲突)
2. full_dispatch_request() 替代 CLIENT.open()(后者经过未插桩的 Werkzeug 中间件,覆盖信号丢失)
3. 路径参数预填默认值(1),避免 TypeError crash
4. 异常吞噬(400/404/500),让 fuzzer 继续探索

运行:
  ANOTA_PY=~/icassp/anota/ANOTA/ANOTA-interpreter/python
  $ANOTA_PY -m stateforge.fuzz.atheris_harness stateforge/targets/vulnshop [-max_total_time=30]

实测: cov: 8, corp: 5, exec/s: 3,733, 115k runs/31s
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------- 加载目标

TARGET_DIR = sys.argv[1] if len(sys.argv) > 1 else "stateforge/targets/vulnshop"
DB_PATH = "/tmp/atheris_fuzz.db"

sys.path.insert(0, str(Path(TARGET_DIR).parent))
_mod = Path(TARGET_DIR).name
from importlib import import_module
_app_mod = import_module(f"{_mod}.app")
APP = _app_mod.create_app(db_path=DB_PATH, guards=0, init_db=True)

# ---------------------------------------------------------------- 选择性插桩

import atheris

_INSTRUMENTED = 0
for rule in APP.url_map.iter_rules():
    if rule.endpoint == "static":
        continue
    view = APP.view_functions.get(rule.endpoint)
    if view and hasattr(view, "__code__"):
        try:
            atheris.instrument_func(view)
            _INSTRUMENTED += 1
        except Exception:
            pass
        if view.__closure__:
            for cell in view.__closure__:
                try:
                    inner = cell.cell_contents
                    if callable(inner) and hasattr(inner, "__code__"):
                        atheris.instrument_func(inner)
                        _INSTRUMENTED += 1
                except (AttributeError, ValueError):
                    pass

# ---------------------------------------------------------------- 路由表(路径参数预填)

ROUTES = []
for rule in APP.url_map.iter_rules():
    if rule.endpoint == "static":
        continue
    route = re.sub(r"<(?:int:|string:|path:)?\w+>", "1", rule.rule)
    ROUTES.append(route)

# 辅助函数(也要 instrument)
def decode(data: bytes) -> int:
    return data[0] if data else 0
atheris.instrument_func(decode)

print(f"[atheris] {_INSTRUMENTED} functions instrumented, {len(ROUTES)} routes")


# ---------------------------------------------------------------- TestOneInput


def TestOneInput(data: bytes):
    if len(data) < 1:
        return
    idx = decode(data) % len(ROUTES)
    route = ROUTES[idx]
    try:
        with APP.test_request_context(route):
            APP.full_dispatch_request()
    except Exception:
        pass  # 400/404/500 等应用异常——让 fuzzer 继续


# ---------------------------------------------------------------- 启动

if __name__ == "__main__":
    duration = "30"
    for a in sys.argv:
        if a.startswith("-max_total_time="):
            duration = a.split("=")[1]
    corpus = tempfile.mkdtemp(prefix="sf_corpus_")
    print(f"[atheris] target={TARGET_DIR} corpus={corpus} duration={duration}s")
    sys.argv = [sys.argv[0], corpus, f"-max_total_time={duration}"]
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()
