"""vulnshop 靶侧生命周期 + 观测器集成测试(P0-A 修复 / P0-B 适配)。

覆盖:
- reset_state() 完整隔离(P0 修复:清绑定 **并** 还原 DB 基线);
- SF_STATE_FILES / SF_MEMORY_RESET / sf_state_observer 声明面;
- SnapshotLifecycle + VulnshopStateObserver 端到端:同一序列两次从基线
  执行 → 相同 verified 观测(验收 1);登录链的 token 绑定在输入后复位;
- 观测词汇与覆盖代理词汇分离(Session.* 运行时不可观测 → 缺位,不伪造)。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

from stateforge.runtime.execution_context import (
    ExecutionContext,
    SnapshotLifecycle,
    lifecycle_from_module,
)
from stateforge.state.runtime_observer import Evidence, StateEvidenceLedger

_TARGET = (Path(__file__).resolve().parents[3] / "targets" / "vulnshop"
           / "fuzz_target.py")


def _load():
    """按 entry_driver 同款机制加载靶模块(独立实例 = 干净先验态)。"""
    spec = importlib.util.spec_from_file_location(
        f"sf_vshop_life_{uuid.uuid4().hex[:8]}", _TARGET)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _users(db_path) -> list[tuple]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT username, role FROM users ORDER BY username").fetchall()
    finally:
        conn.close()


def _register_seq(username: str) -> bytes:
    return json.dumps({"steps": [
        {"method": "POST", "path": "/auth/register",
         "json": {"username": username, "password": "pw"}},
    ]}).encode()


def _login_seq(username: str) -> bytes:
    return json.dumps({"steps": [
        {"method": "POST", "path": "/auth/register",
         "json": {"username": username, "password": "pw"}},
        {"method": "POST", "path": "/auth/login",
         "json": {"username": username, "password": "pw"},
         "extract": {"token": "?token"}},
    ]}).encode()


# ------------------------------------------------------- P0 修复:reset_state

def test_reset_state_restores_db_baseline_not_only_bindings():
    """P0 修复:reset_state 清绑定 + DB 回基线(旧行为:DB 残留跨会话)。"""
    mod = _load()
    mod.target(_register_seq("alice"))
    assert _users(mod._DB) == [("alice", "user")]   # 注册已落库
    mod._BINDINGS["token"] = "t0"                   # 模拟登录绑定残留
    mod.reset_state()
    assert _users(mod._DB) == []                    # DB 回基线(核心断言)
    assert mod._BINDINGS == {}                      # 绑定亦清
    # 基线语义正确性:再注册同名用户不 409(旧残留行为的对照)
    mod.target(_register_seq("alice"))
    assert _users(mod._DB) == [("alice", "user")]


def test_reset_state_raises_when_baseline_template_lost():
    mod = _load()
    tpl = (mod._DB.parent / f"sf_baseline_{mod._DB.name}"
           / "baseline" / "db.tpl")
    tpl.unlink()
    with pytest.raises(RuntimeError, match="基线模板丢失"):
        mod.reset_state()


# ------------------------------------------------------- 声明面 + 端到端

def test_target_module_declares_lifecycle_hooks():
    mod = _load()
    assert [Path(p).name for p in mod.SF_STATE_FILES] == [mod._DB.name]
    assert callable(mod.SF_MEMORY_RESET)
    observer = mod.sf_state_observer()
    assert observer.name == "vulnshop-db"
    # 基线态观测 = 2 张种子优惠券(used=False),零用户零订单
    baseline = observer.observe()
    assert {o.key.predicate for o in baseline} == {"Coupon.used"}
    assert all(o.key.entity in ("SAVE10", "WELCOME5") for o in baseline)
    assert all(o.value is False for o in baseline)
    assert all(o.evidence is Evidence.RUNTIME_VERIFIED for o in baseline)


def test_snapshot_lifecycle_end_to_end_deterministic():
    """验收 1:同一序列从基线执行两次 → 相同 verified 观测;输入后复位。"""
    mod = _load()
    ctx = ExecutionContext(
        lifecycle_from_module(mod, "snapshot"), mod.sf_state_observer())
    r1 = ctx.run(mod.target, _register_seq("alice"))
    assert any(o.key.predicate == "User.exists"
               and o.key.entity == "alice" and o.value is True
               for o in r1.observations)            # 还原前读到真实终态
    assert _users(mod._DB) == []                    # 输入后回基线
    r2 = ctx.run(mod.target, _register_seq("alice"))
    assert {(o.key.render(), o.value) for o in r1.observations} == \
        {(o.key.render(), o.value) for o in r2.observations}
    assert ctx.runs == 2 and not ctx.poisoned


def test_snapshot_lifecycle_resets_token_bindings_per_input():
    """符号绑定表(内存态)在每输入后复位:登录 token 不跨输入泄漏。"""
    mod = _load()
    ctx = ExecutionContext(lifecycle_from_module(mod, "snapshot"))
    ctx.run(mod.target, _login_seq("alice"))        # extract 回填 ?token
    assert mod._BINDINGS == {}                      # 输入后内存态复位
    assert _users(mod._DB) == []                    # DB 亦回基线


def test_observer_vocabulary_separate_from_proxy_atoms():
    """Session.* 在本靶运行时不可观测:verified 词汇无 Session 谓词,
    不以 DB 事实伪造会话证据(覆盖代理词汇另记,见 state_seed_injector)。"""
    mod = _load()
    ctx = ExecutionContext(
        lifecycle_from_module(mod, "snapshot"), mod.sf_state_observer())
    r = ctx.run(mod.target, _register_seq("bob"))
    preds = {o.key.predicate for o in r.observations}
    assert "User.exists" in preds and "User.role=user" in preds
    assert not any(p.startswith("Session.") for p in preds)


def test_ledger_records_verified_evidence_from_context():
    """驱动同款编排:observations → StateEvidenceLedger → payload。"""
    mod = _load()
    ctx = ExecutionContext(
        lifecycle_from_module(mod, "snapshot"), mod.sf_state_observer())
    ledger = StateEvidenceLedger(observer_name=mod.sf_state_observer().name)
    r1 = ctx.run(mod.target, _register_seq("carol"))
    assert len(ledger.record(r1.observations)) > 0
    r2 = ctx.run(mod.target, _register_seq("carol"))   # 相同输入:零新颖
    assert ledger.record(r2.observations) == []
    payload = ledger.to_payload()
    assert payload["observer"] == "vulnshop-db"
    assert any("User.exists{carol@user}" == k for k in payload["verified_atoms"])
