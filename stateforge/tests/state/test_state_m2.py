"""Phase-P3 M2:Transition Discovery tests(动态边 + 静态候选 + cli state)。

S11 fixture:login SET session["uid"](app.py:2)、profile 守卫 uid is
None(app.py:7)、admin 守卫 role == "admin"(app.py:13)。
"""
import json
from pathlib import Path

import pytest

from stateforge.state.domain_builder import build_domains
from stateforge.state.observer import Abstractor, StateRegistry, run_state_analysis
from stateforge.state.predicate_extractor import extract_predicates
from stateforge.state.transition import (
    StaticCandidate,
    TransitionEdge,
    TransitionRecorder,
    static_transition_candidates,
)

S11_FIXTURE = Path(__file__).parents[2] / "fixtures" / "state_micro" / "S11_login_admin"


def _make_recorder() -> TransitionRecorder:
    """S11 谓词 → 域 → Abstractor → TransitionRecorder(挂全局去重 registry)。"""
    domains = build_domains(extract_predicates(S11_FIXTURE))
    return TransitionRecorder(StateRegistry(), Abstractor(domains))


def _key2id(recorder: TransitionRecorder) -> dict:
    return {n.state.canonical_key: n.id
            for n in recorder.registry.nodes.values()}


class TestTransitionEdge:
    def test_frozen_and_dedup_key(self):
        e = TransitionEdge("a|b", "a|c", "login", ["app.py:2"])
        with pytest.raises(Exception):
            e.trigger = "other"          # frozen dataclass:禁改
        assert e.dedup_key == ("a|b", "a|c", "login")


class TestTransitionRecorderDynamic:
    def test_s11_three_nodes_two_edges(self):
        """{} --login--> {uid} --admin--> {uid, role=admin}:3 节点 2 边。"""
        rec = _make_recorder()
        rec.new_session({})
        rec.observe("login", {"uid": 123})
        rec.observe("admin", {"uid": 123, "role": "admin"})
        assert len(rec.registry.nodes) == 3
        k2i = _key2id(rec)
        got = {(k2i[e.from_key], k2i[e.to_key], e.trigger)
               for e in rec.registry.edges.values()}
        assert got == {("S0", "S1", "login"), ("S1", "S2", "admin")}
        assert all(e.evidence == ["dynamic:state-diff"]
                   for e in rec.registry.edges.values())

    def test_edge_dedup_on_replay(self):
        """同一会话序列重放:边按 (from_key, to_key, trigger) 去重。"""
        rec = _make_recorder()
        for _ in range(2):
            rec.new_session({})
            rec.observe("login", {"uid": 123})
            rec.observe("admin", {"uid": 123, "role": "admin"})
        assert len(rec.registry.edges) == 2
        assert len(rec.registry.nodes) == 3      # 节点仍去重,不新增

    def test_new_session_resets_from_node(self):
        """new_session() 后边从新会话起点出发,不沿用上一会话当前节点。"""
        rec = _make_recorder()
        rec.new_session({})
        rec.observe("login", {"uid": 123})       # 会话1:S0→S1
        rec.new_session({})                      # 会话2:当前节点重置为 S0
        rec.observe("admin", {"uid": 123, "role": "admin"})
        k2i = _key2id(rec)
        got = {(k2i[e.from_key], k2i[e.to_key], e.trigger)
               for e in rec.registry.edges.values()}
        assert got == {("S0", "S1", "login"), ("S0", "S2", "admin")}
        assert len(rec.registry.nodes) == 3      # 全局注册表不被会话重置污染

    def test_new_session_no_initial_first_observe_no_edge(self):
        """new_session() 不带初始态:首个 observe 只建立当前节点,不产边。"""
        rec = _make_recorder()
        rec.new_session()
        rec.observe("login", {})
        assert rec.registry.edges == {}
        rec.observe("admin", {"uid": 123, "role": "admin"})
        assert len(rec.registry.edges) == 1

    def test_no_edge_within_same_abstract_state(self):
        """抽象值不变(具体值落同一 OTHER 桶)不产生迁移。"""
        rec = _make_recorder()
        rec.new_session({"uid": 123})            # uid=OTHER
        rec.observe("bump", {"uid": 456})        # uid=456 仍 OTHER
        assert rec.registry.edges == {}


class TestStaticCandidates:
    def test_s11_set_mutation_attributed_to_login(self):
        cands = static_transition_candidates(S11_FIXTURE)
        sets = [c for c in cands if c.op == "set"]
        assert any(c.function == "login" and c.var == "session.uid"
                   and c.evidence[0].endswith("app.py:2") for c in sets)

    def test_s11_route_guards_produce_candidates(self):
        """路由守卫(admin/predicate)也产候选,带 file:line 证据。"""
        cands = static_transition_candidates(S11_FIXTURE)
        guards = [c for c in cands if c.op.startswith("guard:")]
        assert any(c.function == "admin" and c.var == "session.role"
                   and c.op == "guard:EQ(admin)"
                   and c.evidence[0].endswith("app.py:13") for c in guards)
        assert any(c.function == "profile" and c.var == "session.uid"
                   and c.op == "guard:IS_NONE"
                   and c.evidence[0].endswith("app.py:7") for c in guards)

    def test_candidate_to_dict(self):
        d = StaticCandidate("login", "session.uid", "set",
                            ["app.py:2"]).to_dict()
        assert d == {"function": "login", "var": "session.uid",
                     "op": "set", "evidence": ["app.py:2"]}


class TestRunStateAnalysisTransitions:
    def test_transitions_key_gained(self):
        result = run_state_analysis(S11_FIXTURE)
        tr = result["transitions"]
        assert any(c["function"] == "login" and c["op"] == "set"
                   for c in tr["static_candidates"])
        assert "TransitionRecorder" in tr["note"]   # 动态边来自 fuzz 期
        for key in ("accesses", "predicates", "domains",
                    "n_variables", "n_predicates"):  # M1 既有键不动
            assert key in result


class TestCliState:
    def test_state_cmd_end_to_end(self, tmp_path):
        from stateforge.cli import state_cmd
        out = tmp_path / "state.json"
        assert state_cmd(str(S11_FIXTURE), str(out)) == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        for key in ("accesses", "predicates", "domains",
                    "n_variables", "n_predicates", "transitions"):
            assert key in payload
        assert any(c["op"] == "set" and c["function"] == "login"
                   for c in payload["transitions"]["static_candidates"])
