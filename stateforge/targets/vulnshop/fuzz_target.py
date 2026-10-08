"""vulnshop Atheris 靶函数(P4-T3:AppModel 真路径的靶侧消费面)。

双执行面(R7 的消费端契约,PROTOCOLS C10):

1. **请求 dict 面**(序列种子步 / 单请求 dict):`{"method","path",
   "json","query","form","header","extract"}` → Flask
   `test_request_context + full_dispatch_request` 真实全栈。规划器渲染的
   `?token`/`?oid` 符号槽在此运行期代入:`_BINDINGS` 模块级绑定表 +
   `extract` 回填(login 的 token、create 的 id)——会话连续性 = 进程内
   持久(entry_driver.execute_sequence 契约)。json 值纯数字字符串还原
   int(镜像 runtime/replay._coerce:计划模板参数覆写是字符串形态)。

2. **legacy 字节面**(盲变异/既有语料):非 JSON 或非请求 dict 的字节 →
   `adapters/flask.FlaskAdapter` 路由枚举(data[0] 选路),**不带认证、
   不带请求体**——深状态(下单/支付/改角色)对盲字节关闭,只有状态链
   序列种子能解锁;这是 E 臂与 D1 对照的前提(免费认证=混淆因子)。

DB 每进程独立(文件名含 pid+模块名):子进程重启语义下状态不跨轮结转,
与战役 B 臂的重启代价口径一致。GUARDS 环境旗标透传(默认 0=vuln 变体)。
"""

from __future__ import annotations

import atexit
import importlib.util
import json
import os
import re
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent

# 工厂按文件路径加载到私有模块名:顶层名 "app" 可能已被其它先载入的靶
# (如 external/BenchmarkPython/app.py 的 sys.modules 缓存)占据,不能用
# `from app import create_app` 这种全局名解析。
_APP_SPEC = importlib.util.spec_from_file_location(
    f"_vulnshop_app_{os.getpid()}", _HERE / "app.py")
_APP_MOD = importlib.util.module_from_spec(_APP_SPEC)
sys.modules[_APP_SPEC.name] = _APP_MOD
_APP_SPEC.loader.exec_module(_APP_MOD)
create_app = _APP_MOD.create_app

# 每进程/每模块独立 DB(重启语义:状态不跨子进程结转)
_DB = Path(tempfile.gettempdir()) / f"vulnshop_{os.getpid()}_{__name__}.db"

# 退出清理(P5 终清扫):DB 必须活满进程生命周期(fuzz 子进程重启语义
# 依赖它),只在进程退出时删。选型 = atexit + 注册表去重:
# - 本进程各加载形态(`__name__` 不同 → 路径不同)逐路径注册,进程退出
#   时统一 unlink;子进程逐 epoch 新起,各自退出即清自己的 DB;
# - 模块重载(_load 同款 fresh-module 模式重执行本模块)不会误触发清理
#   ——atexit 只在进程退出时到期;重载仅可能重复登记同路径,注册表
#   (模块级 set)在单次加载内去重,跨重载的重复登记由幂等 unlink
#   (缺失忽略)兜底,无害;
# - kill -9 等异常终止不经过 atexit,残留 <pid> 名字含死进程 pid,不由
#   本机制回收(诚实边界:本机制只清自己活时创建的文件)。
_DB_CLEANUP_REGISTERED: set[str] = set()


def _register_db_cleanup(path: Path) -> None:
    if str(path) in _DB_CLEANUP_REGISTERED:
        return
    _DB_CLEANUP_REGISTERED.add(str(path))

    def _cleanup() -> None:
        try:
            path.unlink()
        except OSError:
            pass                    # 已清理/未建成等 → 幂等忽略

    atexit.register(_cleanup)


_register_db_cleanup(_DB)
_APP = create_app(db_path=str(_DB), init_db=True)

_BINDINGS: dict[str, object] = {}          # ?槽 → 运行期值(extract 回填)
_SLOT = re.compile(r"\?([A-Za-z_][A-Za-z0-9_]*)")


def reset_state() -> None:
    """清运行期绑定(测试先验态隔离;DB 不重置——那是靶场状态本身)。"""
    _BINDINGS.clear()


def _sub(value: str) -> str:
    """`?name` → 绑定值;未绑定槽原样保留(404/401 由靶场诚实裁决)。"""
    return _SLOT.sub(
        lambda m: str(_BINDINGS.get(m.group(1), m.group(0))), value)


def _coerce(value):
    """json 体纯数字字符串 → int(镜像 runtime/replay._coerce)。"""
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return value


def _request(step: dict):
    """一个请求 dict → 真实 Flask 全栈;extract 回填 _BINDINGS。"""
    method = str(step.get("method", "GET")).upper()
    path = _sub(str(step.get("path", "/")))
    kwargs: dict = {"method": method,
                    "headers": {str(k): _sub(str(v))
                                for k, v in (step.get("header") or {}).items()}}
    json_body = step.get("json")
    if isinstance(json_body, dict):
        kwargs["json"] = {k: _coerce(v) for k, v in json_body.items()}
    query = step.get("query")
    if isinstance(query, dict):
        kwargs["query_string"] = {k: _coerce(v) for k, v in query.items()}
    with _APP.test_request_context(path, **kwargs):
        resp = _APP.full_dispatch_request()
    extract = step.get("extract") or {}
    if extract and resp.status_code < 400:
        payload = resp.get_json(silent=True)
        if isinstance(payload, dict):
            for key, slot in extract.items():
                val = payload.get(key)
                if val is not None:
                    _BINDINGS[str(slot).lstrip("?")] = val
    return resp


_LEGACY = None                                # 惰性:首次盲字节才枚举路由


def _legacy(data: bytes) -> None:
    """盲字节面:路由枚举、无认证、无请求体(adapters/flask 复用)。"""
    global _LEGACY
    if _LEGACY is None:
        from stateforge.adapters.flask import FlaskAdapter
        _LEGACY = FlaskAdapter(_APP, headers={})
    try:
        _LEGACY.execute(data)
    except Exception:                          # noqa: BLE001  fuzz 不 crash
        pass


def target(data: bytes) -> None:
    try:
        seed = json.loads(data) if data else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _legacy(data)                   # 非 JSON 字节 → legacy 面
    if isinstance(seed, dict) and isinstance(seed.get("steps"), list):
        try:                                    # 与单请求面同款:fuzz 输入
            for step in seed["steps"]:          # 不得把 crash 泄给引擎
                if isinstance(step, dict):
                    _request(step)
        except Exception:                       # noqa: BLE001
            pass
        return
    if isinstance(seed, dict) and isinstance(seed.get("path"), str):
        try:
            _request(seed)                     # C10 单请求 dict 面
        except Exception:                      # noqa: BLE001  fuzz 不 crash
            pass
        return
    _legacy(data)
