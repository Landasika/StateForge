"""P3-T2 序列种子契约（C10）——TDD 先行，本文件先于实现落地。

口径（R1 冻结裁决）：
- 序列种子 = {"steps": [<单请求 dict>, ...]}：前缀步素状态，末步=当前请求；
- legacy 单请求契约（{"query"/"form"/"json"/"header"/"cookie"}）字节不变：
  dispatch 只按 "steps" 键存在性分派，其余输入原字节透传（四臂非回归门）；
- 覆盖 = 序列全程并集：所有步在同一 handler 调用、同一 TraceSession 窗口内执行；
- 会话连续性 = 进程内持久（adapter 持有的 app/client、靶模块级会话态跨调用存活）。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest

from stateforge.analysis.static.icfg import build_icfg
from stateforge.fuzzing.candidates import CandidateCollector
from stateforge.fuzzing.entry_driver import (decode_sequence_seed,
                                             dispatch_seed,
                                             encode_sequence_seed)
from stateforge.fuzzing.feedback.structural import StructuralFeedback
from stateforge.fuzzing.recorder import ExperimentRecorder
from stateforge.runtime.tracer import TraceSession

STATEFUL_DIR = Path(__file__).resolve().parents[2] / "targets" / "stateful"
MICROPLUS_DIR = Path(__file__).resolve().parents[2] / "targets" / "microplus"

# 深块标记（各靶的函数名即命中标记，HITS 逐请求追加）
DEEP_MARKERS = {
    "t_state_chain": "deep_state_profile",
    "t_state_gate": "gate_granted",
    "t_state_shop": "ship_privileged",
}


def _load(name: str):
    """按 entry_driver 同款机制加载靶模块（独立模块实例=干净先验态）。"""
    path = (STATEFUL_DIR / f"{name}.py") if name.startswith("t_state_") else (
        MICROPLUS_DIR / f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"sf_t2_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _seq(*steps: dict) -> bytes:
    return encode_sequence_seed(list(steps))


# ------------------------------------------------------- C10 分派：分派与透传


def test_legacy_passthrough_byte_identical():
    """非序列输入必须原字节透传（同一对象、零重序列化）——legacy 路径字节不变。"""
    seen = []

    def spy(data: bytes) -> None:
        seen.append(data)

    for raw in (b"\x00\xffnot json", b'{"json": {"op": "legacy"}}',
                b"[1,2]", b'"str"', b"5", b'{"steps": "not-a-list"}'):
        dispatch_seed(spy, raw)
        assert seen[-1] is raw, f"legacy 输入被改写: {raw!r}"
    assert len(seen) == 6


def test_sequence_dispatch_executes_steps_in_order():
    """序列输入按序逐步执行，每步=同一 target(data) 执行面。"""
    calls = []

    def spy(data: bytes) -> None:
        calls.append(data)

    dispatch_seed(spy, _seq({"json": {"op": "a"}}, {"json": {"op": "b"}}))
    assert [json.loads(c) for c in calls] == [{"json": {"op": "a"}},
                                              {"json": {"op": "b"}}]


def test_decode_rejects_non_sequence_inputs():
    """decode 只认 dict 且 "steps" 为 list；其余一律 None（→legacy 透传）。"""
    assert decode_sequence_seed(_seq({"json": {}}, {"json": {}})) == [
        {"json": {}}, {"json": {}}]
    assert decode_sequence_seed(b'{"json":{}}') is None
    assert decode_sequence_seed(b'{"steps": 5}') is None
    assert decode_sequence_seed(b'{"steps": "x"}') is None
    assert decode_sequence_seed(b"[1,2]") is None
    assert decode_sequence_seed(b"\x00\xff") is None


# ------------------------------------------- t_state_gate：序列 vs 单请求（旗舰）


def test_gate_sequence_reaches_deep_block_single_request_cannot():
    """同一深块：等价单请求种子打不开 401 门，序列种子（先素态）可打开。"""
    mod = _load("t_state_gate")

    mod.target(json.dumps({"json": {"op": "admin_panel"}}).encode())
    assert mod.HITS == ["unauthorized"]          # 单请求：门未开，深块不可达
    assert mod._SESSION == {}                    # 且未留下任何会话态

    mod.reset_state()
    dispatch_seed(mod.target, _seq(
        {"json": {"op": "login", "user": "root", "role": "admin"}},
        {"json": {"op": "admin_panel"}}))
    assert mod.HITS == ["login_ok", "gate_granted"]   # 序列：先素态后过门
    assert mod._SESSION.get("role") == "admin"


# ------------------------------------------------- t_state_chain：真会话连续性


def test_chain_session_continuity_step2_sees_step1_state():
    """步2 真实读到步1 写入的会话态（不是"步都执行了"的假连续）。"""
    mod = _load("t_state_chain")
    dispatch_seed(mod.target, _seq(
        {"json": {"op": "login", "user": "alice"}},
        {"json": {"op": "profile"}}))
    assert mod._SESSION.get("uid") == "alice"          # 步1 写、步2 读
    assert mod.HITS == ["login_ok", "deep_state_profile"]

    fresh = _load("t_state_chain")                     # 空先验态的同词法输入
    dispatch_seed(fresh.target, _seq({"json": {"op": "profile"}}))
    assert fresh._SESSION == {}
    assert fresh.HITS == ["profile_anon"]              # 深块不可达


# ------------------------------------------------------- t_state_shop：三步链


def test_shop_three_step_chain_reaches_privileged_action():
    """register→create→ship 三步全通才到特权块；乱序/缺步走靶错误路径。"""
    mod = _load("t_state_shop")
    dispatch_seed(mod.target, _seq(
        {"json": {"op": "register", "user": "alice", "password": "pw"}},
        {"json": {"op": "create_order", "item": "widget"}},
        {"json": {"op": "ship_order"}}))
    assert mod.HITS == ["register_ok", "order_created", "ship_privileged"]
    assert mod._DB["orders"]["1"]["owner"] == "alice"

    fresh = _load("t_state_shop")                      # 未注册直接发货
    dispatch_seed(fresh.target, _seq({"json": {"op": "ship_order"}}))
    assert fresh.HITS == ["ship_denied"]


# ------------------------------------------------- 三靶：legacy schema 兼容


@pytest.mark.parametrize("name", ["t_state_chain", "t_state_gate", "t_state_shop"])
def test_all_targets_accept_legacy_single_request(name):
    """三靶接受 legacy 单请求契约：空先验态下行为确定、深块不可达。"""
    mod = _load(name)
    empty = {"query": {}, "form": {}, "json": {}, "header": {}, "cookie": {}}
    mod.target(json.dumps(empty).encode())
    assert mod.HITS == []                              # 空请求=错误路径，零命中

    mod.reset_state()
    prime = {"t_state_chain": ({"op": "login", "user": "u1"}, ["login_ok"]),
             "t_state_gate": ({"op": "login", "user": "root",
                               "role": "admin"}, ["login_ok"]),
             "t_state_shop": ({"op": "register", "user": "u1",
                               "password": "pw"}, ["register_ok"])}[name]
    mod.target(json.dumps({"json": prime[0]}).encode())
    assert mod.HITS == prime[1]                        # 单请求素态行为不变
    assert DEEP_MARKERS[name] not in mod.HITS          # 单请求永远摸不到深块


@pytest.mark.parametrize("name", ["t_state_chain", "t_state_gate", "t_state_shop"])
def test_target_replays_whole_sequence_schema(name):
    """靶本体也接受 {"steps":[...]}（整体重放语义）——与驱动逐步分派等价。"""
    mod = _load(name)
    steps = {"t_state_chain": [{"json": {"op": "login", "user": "alice"}},
                               {"json": {"op": "profile"}}],
             "t_state_gate": [{"json": {"op": "login", "user": "root",
                                        "role": "admin"}},
                              {"json": {"op": "admin_panel"}}],
             "t_state_shop": [{"json": {"op": "register", "user": "alice",
                                        "password": "pw"}},
                              {"json": {"op": "create_order"}},
                              {"json": {"op": "ship_order"}}]}[name]
    mod.target(_seq(*steps))
    assert mod.HITS[-1] == DEEP_MARKERS[name]


@pytest.mark.parametrize("name", ["t_state_chain", "t_state_gate", "t_state_shop"])
def test_reset_state_isolates_sessions(name):
    """reset_state() 清会话/库/命中——测试与多会话场景的隔离契约。"""
    mod = _load(name)
    mod.target(json.dumps({"json": {"op": "login", "user": "x"}}).encode())
    mod.reset_state()
    assert mod._SESSION == {} and mod.HITS == []


# ------------------------------------------------- 畸形序列：确定性错误处理


def test_malformed_sequences_deterministic_no_crash():
    """空 steps=零执行；非 dict 步原样喂靶；均不抛出、行为确定。"""
    seen = []

    def spy(data: bytes) -> None:
        seen.append(data)

    dispatch_seed(spy, _seq())                         # 空 steps
    assert seen == []                                  # 零执行，无异常

    dispatch_seed(spy, b'{"steps": [1, "x", null, {"json": {}}]}')
    assert [json.loads(c) for c in seen] == [1, "x", None, {"json": {}}]


@pytest.mark.parametrize("name", ["t_state_chain", "t_state_gate", "t_state_shop"])
def test_malformed_steps_hit_target_error_path(name):
    """非 dict 步/空 steps 落到靶自己的错误路径：不 crash、HITS 确定。"""
    mod = _load(name)
    mod.target(b'{"steps": [5, "junk", null]}')        # 非 dict 步→靶错误路径
    assert mod.HITS == []
    mod.target(b'{"steps": []}')                       # 空序列=零请求
    assert mod.HITS == []
    mod.target(b'{"steps":')                           # 坏 JSON→legacy 透传→错误路径
    assert mod.HITS == []


# ----------------------------------------------------- 覆盖并集（C3/C4 口径）


def _collector(tmp_path, ir, session, feedback):
    recorder = ExperimentRecorder(tmp_path, group="cov", epoch=0)
    return CandidateCollector(ir, session, feedback, None, recorder, 0)


def test_coverage_union_over_sequence_steps(tmp_path):
    """序列全程覆盖=各步并集：一次输入、一个请求窗口，两步的块都进同一计数。"""
    ir = build_icfg(STATEFUL_DIR)
    mod = _load("t_state_gate")
    session = TraceSession(root=STATEFUL_DIR)
    session.__enter__()
    feedback = StructuralFeedback()
    collector = _collector(tmp_path, ir, session, feedback)

    def dispatch(data: bytes) -> None:                 # entry_driver 同款接线
        dispatch_seed(mod.target, data)

    single = collector.handle(dispatch, b'{"json": {"op": "admin_panel"}}')
    single_fns = {bid.split("::")[1] for bid in single.blocks}
    assert "unauthorized" in single_fns and "gate_granted" not in single_fns

    seq = collector.handle(dispatch, _seq(
        {"json": {"op": "login", "user": "root", "role": "admin"}},
        {"json": {"op": "admin_panel"}}))
    seq_fns = {bid.split("::")[1] for bid in seq.blocks}
    assert {"login_ok", "gate_granted"} <= seq_fns     # 两步的块在同一观测里

    # 全程并集进同一份全局计数（覆盖单调增长，四臂口径不动）
    assert {bid.split("::")[1] for bid in feedback.global_blocks} >= \
        {"login_ok", "gate_granted", "unauthorized"}
    # 一个输入=一个 TraceSession 请求窗口：两步事件共享同一 request_id
    rids = {e.request_id for e in session.events
            if e.function in ("login_ok", "gate_granted")}
    assert len(rids) == 1


# --------------------------------------- 跨输入状态延续（真实 fuzzing 期语义）


def test_state_persists_across_libfuzzer_inputs_until_reset():
    """钉扎：libFuzzer 输入间**无逐输入重置**——输入1 的会话态延续到输入2。

    驱动分派是通用面，不可能知道靶的 reset_state()，因此真实 fuzzing 期
    状态在输入间进程内延续（这正是序列探索状态空间的载体）。对照：需
    干净态的实验必须显式 reset_state()。全程走真实 dispatch 面（与
    entry_driver 同一函数），不直接调靶。
    """
    mod = _load("t_state_gate")

    # 真实 fuzzing 期语义：两次输入之间无任何重置
    dispatch_seed(mod.target, _seq(
        {"json": {"op": "login", "user": "root", "role": "admin"}}))
    dispatch_seed(mod.target, b'{"json": {"op": "admin_panel"}}')
    assert mod.HITS == ["login_ok", "gate_granted"]   # 输入2 借输入1 残留态开门

    # 对照组：reset_state() 之后的同词法输入2 只被门拒
    mod.reset_state()
    dispatch_seed(mod.target, _seq(
        {"json": {"op": "login", "user": "root", "role": "admin"}}))
    mod.reset_state()
    dispatch_seed(mod.target, b'{"json": {"op": "admin_panel"}}')
    assert mod.HITS == ["unauthorized"]               # 干净态：深块不可达


# --------------------------------------------- legacy 靶抽查（四臂非回归门）
def test_legacy_target_spot_check_microplus_deep_branch():
    """legacy 靶（microplus deep_branch，字节门）经 dispatch 行为与裸调一致。"""
    mod = _load("deep_branch")
    hits = []
    mod.deep = lambda: hits.append(1)                  # 观测深块
    dispatch_seed(mod.target, b"B\x17\x99")
    assert hits == [1]                                 # 字节门照常打开
    mod.alt_path = lambda data: hits.append(len(data))
    dispatch_seed(mod.target, b"\x07abc")
    assert hits[-1] == 4                               # 旁路照常
    dispatch_seed(mod.target, b"\x01\x02")             # 非门输入：零命中
    assert len(hits) == 2
