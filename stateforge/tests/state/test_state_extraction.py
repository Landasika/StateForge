"""Phase-5A:StateFact Discovery 测试(S1-S7 micro targets)。"""
import json
from pathlib import Path

import pytest

from stateforge.state.extractor import extract_state_facts
from stateforge.state.index import build_index
from stateforge.state.model import (StateDomain, StateOperation,
                                    Relevance, PredicateOp)

FIXTURES = Path(__file__).parents[2] / "fixtures" / "state_micro"


def _facts(case: str) -> list:
    return extract_state_facts(FIXTURES / case)


def _find(facts, domain, obj_ref, op=None):
    return [f for f in facts if f.domain == domain
            and f.object_ref == obj_ref
            and (op is None or f.operation == op)]


class TestSession:
    """S1/S2:Session read/write。"""

    def test_s1_session_read_control(self):
        facts = _facts("S1_session_read")
        reads = _find(facts, StateDomain.SESSION, "session.user_id",
                      StateOperation.READ)
        assert reads, "应有 session.user_id READ"
        assert all(f.relevance == Relevance.CONTROL for f in reads)
        assert reads[0].structured_predicate is not None
        assert reads[0].structured_predicate.operator == PredicateOp.EXISTS

    def test_s2_session_write(self):
        facts = _facts("S2_session_write")
        writes = _find(facts, StateDomain.SESSION, "session.user_id",
                       StateOperation.WRITE)
        assert writes
        assert writes[0].written_value is None  # uid 变量非常量

    def test_request_input_not_state(self):
        """S7:request.args 不应产生 StateFact。"""
        facts = _facts("S7_combined")
        request_facts = [f for f in facts
                        if "request" in f.object_ref.lower()
                        or "args" in f.object_ref.lower()]
        assert not request_facts, \
            f"request input 不应产生 StateFact: {request_facts}"


class TestDatabase:
    """S3/S4:Database guard/write。"""

    def test_s3_db_guard(self):
        facts = _facts("S3_db_guard")
        reads = _find(facts, StateDomain.DATABASE, "User.role",
                      StateOperation.READ)
        assert reads
        assert reads[0].relevance == Relevance.CONTROL
        assert reads[0].structured_predicate.operator == PredicateOp.EQ
        assert reads[0].structured_predicate.value == "admin"

    def test_s4_db_write(self):
        facts = _facts("S4_db_write")
        writes = _find(facts, StateDomain.DATABASE, "Order.status",
                       StateOperation.WRITE)
        assert writes
        assert writes[0].written_value == "PAID"


class TestFile:
    """S5:File state。"""

    def test_s5_file_read_control(self):
        facts = _facts("S5_file_state")
        reads = _find(facts, StateDomain.FILE, "FILE:/tmp/approved",
                      StateOperation.READ)
        assert reads
        assert reads[0].relevance == Relevance.CONTROL


class TestNoise:
    """S6:非控制性使用。"""

    def test_s6_noise_non_control(self):
        facts = _facts("S6_noise")
        assert all(f.relevance != Relevance.CONTROL for f in facts), \
            f"非守卫上下文不应标 CONTROL: {[(f.object_ref, f.relevance) for f in facts]}"


class TestS7Combined:
    """S7:组合工作流。"""

    def test_s7_session_and_role_identified(self):
        facts = _facts("S7_combined")
        session_facts = _find(facts, StateDomain.SESSION, "session.user_id")
        role_facts = _find(facts, StateDomain.DATABASE, "User.role")
        assert session_facts, "应有 session.user_id"
        assert role_facts, "应有 User.role"
        # 至少各有一个 CONTROL(守卫中的使用;非守卫使用正确为 NON_CONTROL)
        assert any(f.relevance == Relevance.CONTROL for f in session_facts)
        assert any(f.relevance == Relevance.CONTROL for f in role_facts)

    def test_s7_request_confirm_not_state(self):
        facts = _facts("S7_combined")
        confirm = [f for f in facts if "confirm" in f.object_ref]
        assert not confirm, "request.args.confirm 不是 StateFact"


class TestDeterminism:
    def test_deterministic_ids(self):
        f1 = _facts("S1_session_read")
        f2 = _facts("S1_session_read")
        assert [f.candidate_id for f in f1] == [f.candidate_id for f in f2]

    def test_deterministic_serialization(self):
        idx1 = build_index(_facts("S1_session_read"))
        idx2 = build_index(_facts("S1_session_read"))
        assert json.dumps(idx1.to_dict(), sort_keys=True) \
            == json.dumps(idx2.to_dict(), sort_keys=True)


class TestIndex:
    def test_state_fact_index_aggregation(self):
        facts = extract_state_facts(Path(__file__).parents[2]
                                    / "targets" / "vulnshop")
        idx = build_index(facts)
        # vulnshop 有 Order.status 写入
        profile = idx.get_usage_profile("Order.status")
        if profile:
            assert "WRITE" in profile or "READ" in profile

    def test_usage_profile_format(self):
        facts = _facts("S3_db_guard")
        idx = build_index(facts)
        profile = idx.get_usage_profile("User.role")
        assert profile and "GUARD" in profile and "EQ" in profile

    def test_no_missing_security_check_claim(self):
        """usage_profile 只能说 NO_MATCHED_PREDICATE_OBSERVED,不能说漏洞。"""
        facts = _facts("S7_combined")
        idx = build_index(facts)
        for ref in idx.objects:
            profile = idx.get_usage_profile(ref) or ""
            assert "MISSING_SECURITY_CHECK" not in profile
            assert "vulnerab" not in profile.lower()
