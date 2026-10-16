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
import shutil
import sys
import tempfile
from pathlib import Path

from stateforge.runtime.snapshot.l2_sqlite import L2Sqlite

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


def _sweep_dead_pid_dbs() -> None:
    """死 pid 机会清扫(P5-T3 修复轮 1):删 owner 已死的残留 DB。

    生产路径的 epoch 子进程经 atheris.Fuzz() 由 libFuzzer _Exit 终止
    (entry_driver.py:4/:188,recorder.py:3——两类产物靠逐行 flush 存活,
    正是为此),finally/atexit 被绕过,故 atexit 钩子在该路径**不到期**;
    本清扫在模块载入(新进程本就要重建同形状态)时按名字里的 pid 判活
    补删,同时覆盖 kill -9。存活判定 os.kill(pid,0):
    ProcessLookupError=死(删);PermissionError(EPERM)=在(他人所有,
    不动);零信号成功=在(不动)。pid 复用 → 误判存活 → 留残文件
    (保守不删)。纯靶内机制,evaluation 侧零耦合。
    """
    for cand in Path(tempfile.gettempdir()).glob("vulnshop_*_*.db"):
        owner = cand.name.split("_")[1]        # 形如 vulnshop_<pid>_<mod>.db
        if not owner.isdigit():
            continue
        try:
            os.kill(int(owner), 0)
        except ProcessLookupError:
            pass                               # 死 pid → 下方删除
        except OSError:
            continue                           # EPERM = 存活(他人所有)→ 不动
        else:
            continue                           # 存活 → 不动
        try:
            cand.unlink()
        except OSError:
            pass                               # 竞态/权限 → 幂等忽略


_sweep_dead_pid_dbs()

# 干净退出补扫(P5 终清扫):体面退出(如测试进程)立刻删自己的 DB,
# 不等下一个进程的清扫。注意生产路径(entry_driver 子进程)经
# atheris.Fuzz() 的 libFuzzer _Exit 绕过 atexit——该钩子只覆盖干净退出;
# epoch 子进程的 DB 由上方死 pid 清扫回收(机制互补,注释在 _sweep)。
# 模块重载(_load 同款 fresh-module 模式重执行本模块)不会误触发清理
# ——atexit 只在进程退出时到期;重载仅可能重复登记同路径,注册表
# (模块级 set)在单次加载内去重,跨重载的重复登记由幂等 unlink
# (缺失忽略)兜底,无害。
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
        shutil.rmtree(path.parent / f"sf_baseline_{path.name}",
                      ignore_errors=True)   # P0-A 基线模板目录同清理

    atexit.register(_cleanup)


_register_db_cleanup(_DB)
_APP = create_app(db_path=str(_DB), init_db=True)

# P0-A 修复(阶段 1):DB 基线模板 = init 后干净态(2 张种子优惠券、
# 零用户零订单)。reset_state()/SnapshotLifecycle 经 L2Sqlite 位级拷贝
# 还原(ADR-0004;-journal/-wal/-shm 一并清理)。模板根独立于
# runtime/snapshot 监督进程的 tpl/,互不干扰。
_L2 = L2Sqlite(_DB, tpl_root=_DB.parent / f"sf_baseline_{_DB.name}")
_L2.snapshot_template("baseline")

_BINDINGS: dict[str, object] = {}          # ?槽 → 运行期值(extract 回填)
_SLOT = re.compile(r"\?([A-Za-z_][A-Za-z0-9_]*)")


def reset_bindings() -> None:
    """内存态恢复钩子:只清符号绑定(文件态由 L2 模板负责)。"""
    _BINDINGS.clear()


def reset_state() -> None:
    """完整先验态隔离(阶段 1 P0 修复):清符号绑定 **并** 还原 DB 基线。

    旧版只清 _BINDINGS、DB 跨调用延续("那是靶场状态本身")——审计 P0
    裁定该语义与既有约定(plan_executor/goal_directed_eval 的每计划冷
    启动、三微靶 reset_state 的清会话/库/命中)不一致,且导致计划间/
    会话间 DB 泄漏(如上轮注册的用户名使本轮 409)。基线丢失 = 隔离
    失效 → 显式 RuntimeError(不静默跑在污染态上)。
    """
    reset_bindings()
    if not _L2.template_exists("baseline"):
        raise RuntimeError(
            f"vulnshop DB 基线模板丢失: {_L2.root}/baseline/db.tpl")
    _L2.restore("baseline")


# ---- P0-A/P0-B 靶侧声明面(execution_context.lifecycle_from_module 约定):
# SF_STATE_FILES:文件态基线声明(snapshot 生命周期捕获/还原的文件集);
# SF_MEMORY_RESET:内存态恢复钩子(→ establish 时刻的绑定空表);
# sf_state_observer():运行时观测器工厂(无则驱动显式退化到代理模式)。
SF_STATE_FILES = [str(_DB)]
SF_MEMORY_RESET = reset_bindings


def sf_state_observer():
    from stateforge.targets.vulnshop.state_evidence import VulnshopStateObserver
    return VulnshopStateObserver(_DB)


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
