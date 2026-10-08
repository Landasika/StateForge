"""Phase-4.2-D 测试:proposal 解析 / validator 六项 / 三类 micro 场景 / trace。"""
import json
from pathlib import Path

import pytest

from stateforge.solving.llm_solver import LLMSemanticSolver, _parse_llm_output
from stateforge.solving.proposal import MutationProposal, ProposalStatus
from stateforge.solving.proposal_trace import ProposalTraceRecorder
from stateforge.solving.proposal_validator import validate

LS = Path(__file__).parents[2] / "fixtures" / "ls_micro"


# ------------------------------------------------------- proposal 解析

def test_parse_valid_proposed():
    raw = json.dumps({"status": "PROPOSED", "source_kind": "QUERY",
                      "source_key": "name", "operation": "REPLACE",
                      "proposed_value": "admin",
                      "rationale": "concat", "confidence": 0.9})
    p = _parse_llm_output(raw, "f1")
    assert p.status == ProposalStatus.PROPOSED
    assert p.source_kind == "QUERY" and p.proposed_value == "admin"


def test_parse_invalid_json():
    p = _parse_llm_output("not json at all", "f1")
    assert p.status == ProposalStatus.INVALID_OUTPUT


def test_parse_markdown_fenced():
    raw = '```json\n{"status":"STATE_REQUIRED","source_kind":"",\
"source_key":"","operation":"","proposed_value":null,\
"rationale":"session","confidence":0.0}\n```'
    p = _parse_llm_output(raw, "f1")
    assert p.status == ProposalStatus.STATE_REQUIRED


# ------------------------------------------------------- validator

def _slice(kind="QUERY", key="name", frontier="u->v"):
    return {
        "target_frontier": {"edge": frontier.split("->"), "polarity": "true",
                            "source_hit_count": 200},
        "guard": {"expression": "key == 'user_admin'"},
        "local_cfg": {"function": "run"},
        "backward_dataflow": ["name = request.args['name']"],
        "input_source": {"kind": kind, "key": key,
                         "transform_chain": [], "status": "ok"},
        "runtime": {"observations_seen": 5, "distance_history": []},
        "failed_mutations": [],
    }


def test_validator_passes_legitimate():
    p = MutationProposal("u->v", "QUERY", "name", "REPLACE", "admin")
    v = validate(p, _slice())
    assert v.valid


def test_validator_rejects_wrong_source_key():
    p = MutationProposal("u->v", "QUERY", "password", "REPLACE", "x")
    v = validate(p, _slice())
    assert not v.valid and v.rejection_reason == "SOURCE_KEY_MISMATCH"


def test_validator_rejects_wrong_frontier():
    p = MutationProposal("x->y", "QUERY", "name", "REPLACE", "admin")
    v = validate(p, _slice())
    assert not v.valid and v.rejection_reason == "FRONTIER_MISMATCH"


def test_validator_rejects_bad_operation():
    p = MutationProposal("u->v", "QUERY", "name", "DELETE_ALL", "admin")
    v = validate(p, _slice())
    assert not v.valid and v.rejection_reason == "OPERATION_NOT_ALLOWED"


def test_validator_rejects_duplicate_failed():
    sl = _slice()
    sl["failed_mutations"] = [{"input_id": "admin"}]
    p = MutationProposal("u->v", "QUERY", "name", "REPLACE", "admin")
    v = validate(p, sl)
    assert not v.valid and v.rejection_reason == "DUPLICATES_FAILED_MUTATION"


# ------------------------------------------------------- 三类 micro

class MockProvider:
    """可控 mock:返回预设 LLM 输出(测试不依赖真 API)。"""

    def __init__(self, response: str):
        self.response = response
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        return self.response


def _llm_response(status="PROPOSED", kind="QUERY", key="name", value="admin",
                  confidence=0.9):
    return json.dumps({"status": status, "source_kind": kind,
                       "source_key": key, "operation": "REPLACE",
                       "proposed_value": value,
                       "rationale": "from slice", "confidence": confidence})


def test_class1_deterministic_sufficient_llm_not_triggered(tmp_path):
    """LS01:x==731 确定性 solver 直接解决 → LLM 不该被调。"""
    from stateforge.solving.solve_loop import solve_loop
    from stateforge.analysis.static.icfg import build_icfg
    d = LS / "LS01_deterministic_sufficient"
    res = solve_loop(d / "app.py", build_icfg(d),
                     json.loads((d / "input.json").read_text()))
    # 确定性求解成功(4.1 冻结链)
    assert res.target_reached
    # StagnationMonitor 不触发(DeterministicFailure=False)
    from stateforge.solving.stagnation import StagnationMonitor
    mon = StagnationMonitor(build_icfg(d), d / "app.py", window=50,
                            min_hits=10)
    mon.deterministic_failure = False
    t = mon.observe(100, {"fake": 100}, set(), 0)
    assert t.plateau in ("RUNNING", "STAGNATED") and not t.triggered


def test_class2_llm_proposal_validated_and_executed(tmp_path):
    """LS02:user_+name 拼接(确定性 UNRESOLVED)→ LLM 解 name=admin。"""
    from stateforge.solving.solve_loop import Request, _execute
    from stateforge.solving.branch_observer import load_observed_target
    from stateforge.runtime.tracer import TraceSession
    from stateforge.runtime.mapper import map_trace
    from stateforge.fuzzing.constraint_mutator import apply_hint
    from stateforge.solving.mutation_hint import MutationHint as MH

    d = LS / "LS02_string_concat"
    mod, obs = load_observed_target(d / "app.py")
    seed = json.loads((d / "input.json").read_text())

    # 1) 确定性 solver 在此靶上拒绝(拼接 = UNRESOLVED_TRANSFORM)
    from stateforge.solving.solve_loop import solve_loop
    from stateforge.analysis.static.icfg import build_icfg
    res = solve_loop(d / "app.py", build_icfg(d), seed)
    hints = [e for e in res.events if e["stage"] == "HINT_GENERATED"]
    assert hints and hints[0]["hint_reason"] == "NOT_SINGLE_INPUT_SOLVABLE"

    # 2) LLM 被 mock 为正确 proposal:QUERY["name"] := "admin"
    from stateforge.solving.stagnation import StagnationMonitor
    provider = MockProvider(_llm_response(value="admin"))
    solver = LLMSemanticSolver(provider)

    # 构造 slice(复用 StagnationMonitor 产切片)
    ir = build_icfg(d)
    mon = StagnationMonitor(ir, d / "app.py", window=10, min_hits=5)
    branch_id = next(b.id for b in ir.blocks.values()
                     if b.kind == "branch" and b.function == "run")
    mon.deterministic_failure = True
    session = TraceSession(root=str(d))
    session.__enter__()
    obs_mod, observer = load_observed_target(d / "app.py")
    _execute(obs_mod, observer, session, seed)
    session.__exit__(None, None, None)
    mon.observations = observer.records
    t = None
    for i in range(1, 50):                      # 覆盖稳定 → 窗口后 plateau
        t = mon.observe(i, {branch_id: 30}, set(), 0)
    assert t.triggered and t.slices, f"plateau={t.plateau} stalled={t.stalled_count}"

    # 3) LLM → proposal → validator → candidate → 执行
    proposal, latency, tokens = solver.solve(t.slices[0])
    assert proposal.status == ProposalStatus.PROPOSED
    v = validate(proposal, t.slices[0])
    assert v.valid

    hint = MH(frontier_id=proposal.frontier_id,
              guard_id="llm", source_kind=proposal.source_kind,
              source_key=proposal.source_key,
              current_value="Tom", target_value=proposal.proposed_value,
              operator="Eq", desired_polarity="true",
              reason="solved", confidence=proposal.confidence)
    candidate = apply_hint(seed, hint)
    assert candidate["query"]["name"] == "admin"

    # 4) 真实执行 + mapper 裁决
    from stateforge.solving.solve_loop import solve_loop as _sl
    res2 = _sl(d / "app.py", build_icfg(d), candidate)
    crossed = [e for e in res2.events if e["stage"] == "FRONTIER_CROSSED"]
    assert res2.target_reached, f"LLM proposal 应真实跨越: {res2.events}"


def test_class3_state_required_not_hardcoded():
    """LS04:session guard → LLM 应返回 STATE_REQUIRED,不产假 proposal。"""
    provider = MockProvider(_llm_response(status="STATE_REQUIRED",
                                          kind="", key="",
                                          value=None, confidence=0.0))
    solver = LLMSemanticSolver(provider)
    fake_slice = _slice()  # 任意 slice
    proposal, _, _ = solver.solve(fake_slice)
    assert proposal.status == ProposalStatus.STATE_REQUIRED
    # STATE_REQUIRED 不进 validator→不产 candidate
    assert proposal.proposed_value is None


# ------------------------------------------------------- trace

def test_proposal_trace_roundtrip(tmp_path):
    path = tmp_path / "traces" / "pt.jsonl"
    rec = ProposalTraceRecorder(path)
    rec.record(trigger_reason="plateau+stalled+detfail",
               frontier_id="u->v", slice_hash="abc",
               llm_status="PROPOSED", validation_status="valid",
               distance_before=711, distance_after=0,
               frontier_crossed=True, latency_seconds=1.2)
    rec.close()
    line = json.loads(path.read_text().splitlines()[0])
    assert line["frontier_id"] == "u->v" and line["frontier_crossed"]
    assert line["distance_before"] == 711 and line["distance_after"] == 0
