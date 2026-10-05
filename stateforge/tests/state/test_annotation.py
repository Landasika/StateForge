"""Phase-5B 测试:Mock Annotator + Validator + S8-S10。"""
import json
from pathlib import Path

import pytest

from stateforge.state.annotation.backends.mock import mock_annotate
from stateforge.state.annotation.model import (
    AbstractStateValue, AnnotationSource, RepositoryEvidence, StateAnnotation,
    AnnotationStatus)
from stateforge.state.annotation.validator import validate_annotation
from stateforge.state.extractor import extract_state_facts
from stateforge.state.index import build_index

FIXTURES = Path(__file__).parents[2] / "fixtures" / "state_micro"


def _index(case: str):
    facts = extract_state_facts(FIXTURES / case)
    return build_index(facts)


class TestMockAnnotator:
    def test_mock_produces_annotation(self):
        idx = _index("S8_order_lifecycle")
        # 找 Order.status 或 Orders.status
        obj_ref = next((ref for ref in idx.objects if "status" in ref.lower()
                         and "order" in ref.lower()), None)
        if obj_ref is None:
            pytest.skip("S8 未产生 Order.status")
        entry = idx.objects[obj_ref]
        annotation = mock_annotate(entry, FIXTURES / "S8_order_lifecycle")

        assert annotation.object_ref == obj_ref
        assert annotation.backend == "mock"
        assert annotation.status == AnnotationStatus.SUCCESS
        assert annotation.writers or annotation.readers

    def test_mock_deterministic(self):
        idx = _index("S8_order_lifecycle")
        for ref, entry in idx.objects.items():
            a1 = mock_annotate(entry, FIXTURES / "S8_order_lifecycle")
            a2 = mock_annotate(entry, FIXTURES / "S8_order_lifecycle")
            assert json.dumps(a1.to_dict(), sort_keys=True) \
                == json.dumps(a2.to_dict(), sort_keys=True)


class TestValidator:
    def _make_annotation(self, obj_ref="Order.status",
                         file="app.py", line_start=1, line_end=5,
                         claim="writes status"):
        return StateAnnotation(
            candidate_id=obj_ref, object_ref=obj_ref,
            writers=[__import__(
                "stateforge.state.annotation.model", fromlist=[""
            ]).StateWriterAnnotation(
                function="pay", written_value="PAID",
                evidence=[RepositoryEvidence(
                    file=file, line_start=line_start,
                    line_end=line_end, claim=claim)])],
            backend="mock")

    def test_valid_evidence(self, tmp_path):
        (tmp_path / "app.py").write_text("def pay():\n    status = 'PAID'\n")
        ann = self._make_annotation(file="app.py", line_start=1, line_end=2)
        result = validate_annotation(ann, tmp_path)
        assert result.valid

    def test_invalid_file_rejected(self, tmp_path):
        ann = self._make_annotation(file="nonexistent.py")
        result = validate_annotation(ann, tmp_path)
        assert not result.valid
        assert any(e["reason"] == "file_not_found" for e in
                   result.invalid_evidence)

    def test_invalid_line_range_rejected(self, tmp_path):
        (tmp_path / "app.py").write_text("line1\nline2\n")
        ann = self._make_annotation(file="app.py", line_start=0, line_end=99)
        result = validate_annotation(ann, tmp_path)
        assert not result.valid

    def test_binding_mismatch(self, tmp_path):
        (tmp_path / "app.py").write_text("code\n")
        ann = StateAnnotation(
            candidate_id="Order.status", object_ref="Different.ref",
            backend="mock")
        result = validate_annotation(ann, tmp_path)
        assert not result.binding_valid


class TestS8S9S10:
    def test_s8_order_lifecycle_extraction(self):
        idx = _index("S8_order_lifecycle")
        assert len(idx.objects) >= 1
        # 找一个 status 对象
        status_refs = [r for r in idx.objects if "status" in r.lower()]
        assert status_refs, "S8 应有 status 状态"
        for ref in status_refs:
            profile = idx.get_usage_profile(ref)
            assert profile and "WRITE" in profile

    def test_s9_user_role_extraction(self):
        idx = _index("S9_user_role")
        role_refs = [r for r in idx.objects if "role" in r.lower()]
        assert role_refs, "S9 应有 role 状态"

    def test_s10_file_workflow_extraction(self):
        idx = _index("S10_file_workflow")
        file_refs = [r for r in idx.objects if "FILE" in r]
        if file_refs:
            assert any("WRITE" in (idx.get_usage_profile(r) or "")
                      for r in file_refs)


class TestSerializer:
    def test_annotation_json_output(self, tmp_path):
        idx = _index("S8_order_lifecycle")
        for ref, entry in idx.objects.items():
            ann = mock_annotate(entry, FIXTURES / "S8_order_lifecycle")
            data = ann.to_dict()
            assert isinstance(data, dict)
            assert data["object_ref"] == ref
            json.dumps(data)  # 可序列化
