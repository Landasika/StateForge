"""采集器 × ExecutionContext 接线测试(P0-A/P0-B 在 fuzzing 域的消费面)。

覆盖(Candidate/Frontier 两采集器同形接线,以 Candidate 为主、Frontier
抽查关键路径):
- 默认(execution=None):Noop 语义,handle 行为与旧版一致;
- 生命周期失败:审计落 lifecycle_failures.jsonl,该输入不计执行/覆盖/
  入语料(fail-closed);
- 观测编排:verified 观测进台账(与覆盖反馈并记,不替代);
- 观测/还原帧不进覆盖计数(trace 窗口外执行)。
"""

from __future__ import annotations

import json
from pathlib import Path

from stateforge.analysis.static.icfg import build_icfg
from stateforge.fuzzing.candidates import CandidateCollector
from stateforge.fuzzing.corpus_manager import CorpusManager
from stateforge.fuzzing.feedback.frontier_feedback import FrontierDirectedFeedback
from stateforge.fuzzing.feedback.structural import StructuralFeedback
from stateforge.fuzzing.frontier_collector import FrontierCollector
from stateforge.fuzzing.recorder import ExperimentRecorder
from stateforge.runtime.execution_context import (
    ExecutionContext,
    ResetLifecycle,
)
from stateforge.runtime.tracer import TraceSession
from stateforge.state.runtime_observer import (
    Evidence,
    SCOPE_GLOBAL,
    StateEvidenceLedger,
    StateKey,
    StateObservation,
)

APP_SRC = "def target(data):\n    x = 1\n    return x\n"


class StubObserver:
    name = "stub"

    def observe(self):
        return [StateObservation(StateKey("Stub.state", "*", SCOPE_GLOBAL),
                                 True, Evidence.RUNTIME_VERIFIED, "stub")]


def _env(tmp_path: Path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "app.py").write_text(APP_SRC, encoding="utf-8")
    ir = build_icfg(root)
    session = TraceSession(root=root)
    session.__enter__()
    feedback = StructuralFeedback()
    recorder = ExperimentRecorder(tmp_path / "out", group="w", epoch=1)
    return ir, session, feedback, recorder


def _target_ok(data: bytes) -> None:
    pass


def test_default_execution_keeps_legacy_handle_behavior(tmp_path):
    """execution=None → 内部 Noop:handle 语义与旧版一致(返回 obs、计执行)。"""
    ir, session, feedback, recorder = _env(tmp_path)
    collector = CandidateCollector(ir, session, feedback, None, recorder, 1)
    obs = collector.handle(_target_ok, b"\x00")
    assert obs is not None and recorder.executions == 1
    assert collector.execution.isolation == "none"


def test_lifecycle_failure_audits_and_skips_input(tmp_path):
    """fail-closed:begin 失败 → None + 审计;不计执行、无覆盖、不入语料。"""
    ir, session, feedback, recorder = _env(tmp_path)

    def bad_reset() -> None:
        raise RuntimeError("reset exploded")

    ctx = ExecutionContext(ResetLifecycle([bad_reset]))
    corpus = CorpusManager(tmp_path / "spool")
    collector = CandidateCollector(ir, session, feedback, corpus, recorder, 1,
                                   execution=ctx)
    assert collector.handle(_target_ok, b"\x01") is None
    assert recorder.executions == 0                   # 未执行,不计数
    assert feedback.global_blocks == set()            # 无覆盖记账
    seeds = tmp_path / "spool" / "seeds"              # 不入语料
    assert not seeds.exists() or not list(seeds.glob("*"))
    audit = tmp_path / "out" / "lifecycle_failures.jsonl"
    rec = json.loads(audit.read_text(encoding="utf-8").splitlines()[0])
    assert rec["lifecycle"] == "reset" and "reset exploded" in rec["error"]
    assert collector.lifecycle_failures == 1
    assert collector.handle(_target_ok, b"\x02") is None   # poisoned 持续拒绝


def test_verified_observations_flow_into_ledger(tmp_path):
    """观测编排:finish_run 的 verified 观测 → 台账;覆盖反馈照旧。"""
    ir, session, feedback, recorder = _env(tmp_path)
    ctx = ExecutionContext(observer=StubObserver())
    ledger = StateEvidenceLedger(observer_name="stub")
    collector = CandidateCollector(ir, session, feedback, None, recorder, 1,
                                   execution=ctx, state_ledger=ledger)
    obs = collector.handle(_target_ok, b"\x03")
    assert obs is not None and recorder.executions == 1
    assert ledger.verified_keys == {"Stub.state{*@global}"}


def test_observer_frames_do_not_enter_coverage(tmp_path):
    """观测器帧不入覆盖:观测器文件在 root 内,其执行不产覆盖事件
    (finish_run 在 trace 窗口外调用——防观测膨胀覆盖口径)。"""
    root = tmp_path / "app"
    root.mkdir()
    (root / "app.py").write_text(APP_SRC, encoding="utf-8")
    # 观测器源码也放在 root 内:若被 trace,会映射出覆盖事件
    (root / "observer_impl.py").write_text(
        "def probe():\n    return 42\n", encoding="utf-8")
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location("sf_obs_impl",
                                                  root / "observer_impl.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    class RootObserver:
        name = "root-impl"

        def observe(self):
            mod.probe()                              # root 内帧,若被 trace 即泄漏
            return []

    ir = build_icfg(root)
    session = TraceSession(root=root)
    session.__enter__()
    feedback = StructuralFeedback()
    recorder = ExperimentRecorder(tmp_path / "out", group="v", epoch=1)
    ctx = ExecutionContext(observer=RootObserver())
    collector = CandidateCollector(ir, session, feedback, None, recorder, 1,
                                   execution=ctx)
    collector.handle(_target_ok, b"\x04")
    covered_fns = {b.split("::")[1] for b in feedback.global_blocks}
    assert "probe" not in covered_fns                # 观测帧零覆盖


def test_frontier_collector_same_wiring(tmp_path):
    """Frontier 采集器同形接线抽查:lifecycle 失败 fail-closed + 审计。"""
    root = tmp_path / "app"
    root.mkdir()
    (root / "app.py").write_text(APP_SRC, encoding="utf-8")
    ir = build_icfg(root)
    session = TraceSession(root=root)
    session.__enter__()
    feedback = FrontierDirectedFeedback(ir)
    recorder = ExperimentRecorder(tmp_path / "out2", group="fw", epoch=1)
    corpus = CorpusManager(tmp_path / "spool2")

    def bad_reset() -> None:
        raise RuntimeError("nope")

    ctx = ExecutionContext(ResetLifecycle([bad_reset]))
    collector = FrontierCollector(ir, session, feedback, corpus, recorder, 1,
                                  execution=ctx)
    assert collector.handle(_target_ok, b"\x05") is None
    assert recorder.executions == 0
    assert (tmp_path / "out2" / "lifecycle_failures.jsonl").exists()
