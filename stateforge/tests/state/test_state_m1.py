"""Phase-4.3 M1:State Node Discovery tests(session state)."""
from pathlib import Path

import pytest

from stateforge.state.semantic_model import (
    StateVariable, StatePredicate, StateOp, PredicateOp, StateSource)
from stateforge.state.session_access import analyze_session_access
from stateforge.state.predicate_extractor import extract_predicates
from stateforge.state.domain_builder import build_domains
from stateforge.state.observer import Abstractor, StateRegistry

FIXTURES = Path(__file__).parents[2] / "fixtures" / "state_micro"

S11_FIXTURE = Path(__file__).parents[2] / 'fixtures' / 'state_micro' / 'S11_login_admin'


def _setup(tmp_path, source):
    d = tmp_path / "app"
    d.mkdir(exist_ok=True)
    (d / "app.py").write_text(source, encoding="utf-8")
    return d


class TestSessionAccess:
    def test_read_subscript(self):
        accesses = analyze_session_access(FIXTURES / "S1_session_read")
        reads = [a for a in accesses if a.operation == StateOp.READ]
        assert any(a.variable.canonical == "session.user_id" for a in reads)

    def test_write(self):
        accesses = analyze_session_access(FIXTURES / "S2_session_write")
        writes = [a for a in accesses if a.operation == StateOp.SET]
        assert writes
        assert writes[0].variable.canonical == "session.user_id"


class TestPredicateExtractor:
    def test_eq_predicate(self, tmp_path):
        d = S11_FIXTURE
        preds = extract_predicates(d)
        eq = [p for p in preds if p.op == PredicateOp.EQ
              and p.variable.canonical == "session.role"]
        assert eq and eq[0].value == "admin"

    def test_is_none_predicate(self, tmp_path):
        d = S11_FIXTURE
        preds = extract_predicates(d)
        none = [p for p in preds if p.variable.canonical == "session.uid"
                and p.op == PredicateOp.IS_NONE]
        assert none

    def test_provenance_chain(self, tmp_path):
        src = (
            "def check(session):\n"
            "    role = session.get(\"role\")\n"
            "    x = role\n"
            "    if x == \"admin\":\n"
            "        return True\n"
            "    return False\n"
        )
        d = _setup(tmp_path, src)
        preds = extract_predicates(d)
        assert any(p.variable.canonical == "session.role" and
                   p.op == PredicateOp.EQ and p.value == "admin"
                   for p in preds)


class TestDomainBuilder:
    def test_eq_domain(self):
        preds = [
            StatePredicate(StateVariable(StateSource.SESSION, "role"), PredicateOp.EQ, "admin"),
            StatePredicate(StateVariable(StateSource.SESSION, "role"), PredicateOp.EQ, "manager"),
        ]
        domains = build_domains(preds)
        d = domains["session.role"]
        assert d.abstract("admin") == "EQ(admin)"
        assert d.abstract("manager") == "EQ(manager)"
        assert d.abstract("user") == "OTHER"

    def test_none_domain(self):
        preds = [StatePredicate(StateVariable(StateSource.SESSION, "uid"), PredicateOp.IS_NONE)]
        domains = build_domains(preds)
        d = domains["session.uid"]
        assert d.abstract(None) == "NONE"
        assert d.abstract(123) == "OTHER"

    def test_ge_range(self):
        preds = [
            StatePredicate(StateVariable(StateSource.SESSION, "retry"), PredicateOp.GE, 3),
            StatePredicate(StateVariable(StateSource.SESSION, "retry"), PredicateOp.GE, 5),
        ]
        domains = build_domains(preds)
        d = domains["session.retry"]
        assert d.abstract(5) == ">=3"  # first matching GE rule
        assert d.abstract(4) == ">=3"
        assert d.abstract(2) == "OTHER"


class TestAbstractorRegistry:
    def test_abstract_state(self):
        preds = [
            StatePredicate(StateVariable(StateSource.SESSION, "uid"), PredicateOp.IS_NONE),
            StatePredicate(StateVariable(StateSource.SESSION, "role"), PredicateOp.EQ, "admin"),
        ]
        domains = build_domains(preds)
        abs_ = Abstractor(domains)
        state = abs_.abstract({"uid": 123, "role": "admin"})
        assert state.to_dict() == {"session.uid": "OTHER", "session.role": "EQ(admin)"}

    def test_registry_dedup(self):
        preds = [StatePredicate(StateVariable(StateSource.SESSION, "role"), PredicateOp.EQ, "admin")]
        domains = build_domains(preds)
        abs_ = Abstractor(domains)
        reg = StateRegistry()
        s1 = abs_.abstract({"role": "user"})
        n1 = reg.observe(s1, 0)
        s2 = abs_.abstract({"role": "user"})
        n2 = reg.observe(s2, 1)
        assert n1.id == n2.id
        s3 = abs_.abstract({"role": "admin"})
        n3 = reg.observe(s3, 2)
        assert n3.id != n1.id

    def test_e2e_state_discovery(self, tmp_path):
        d = S11_FIXTURE
        preds = extract_predicates(d)
        domains = build_domains(preds)
        abs_ = Abstractor(domains)
        reg = StateRegistry()
        reg.observe(abs_.abstract({}), 0)
        reg.observe(abs_.abstract({"uid": 123}), 1)
        reg.observe(abs_.abstract({"uid": 123, "role": "admin"}), 2)
        assert len(reg.nodes) == 3
        assert {n.state.to_dict().get("session.role") for n in reg.nodes.values()} == {"OTHER", "EQ(admin)"}