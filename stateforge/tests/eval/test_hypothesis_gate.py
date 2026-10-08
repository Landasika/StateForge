"""v0.7b HVG 测试:Hypothesis 生成 + purity + 三臂评分。"""
import json
from pathlib import Path

import pytest

from stateforge.evaluation.hypothesis_gate import (
    Hypothesis, VerificationResult, Verdict,
    annotate_ground_truth, build_hypotheses_from_analyzer,
    hypothesis_to_prompt, hypothesis_to_bundled_prompt,
    repository_audit_prompt, score_hva)


FIXTURE = Path(__file__).parents[2] / "targets" / "vulnshop"


class TestHypothesisGeneration:
    def test_generates_hypotheses_from_vulnshop(self):
        hyps = build_hypotheses_from_analyzer(FIXTURE)
        assert len(hyps) >= 5, f"vulnshop 应产生 ≥5 个 hypothesis: {len(hyps)}"
        for h in hyps:
            assert h.hid.startswith("H")
            assert h.candidate
            assert h.source
            assert h.endpoint

    def test_hypothesis_purity_no_vuln_labels(self):
        """Hypothesis 文本不得泄漏 vulnerability label。"""
        hyps = build_hypotheses_from_analyzer(FIXTURE)
        forbidden = ["vulnerab", "exploit", "unsafe", "dangerous",
                     "injection", "bypass", "likely"]
        for h in hyps:
            text = h.candidate.lower()
            for word in forbidden:
                assert word not in text, \
                    f"{h.hid} candidate 泄漏 '{word}': {h.candidate}"

    def test_prompt_isolated_format(self):
        h = Hypothesis(
            hid="H001", candidate="Test candidate",
            source="app.py:10", sink="query", known_flow="a→b",
            known_guards=["auth"], endpoint="POST /test")
        prompt = hypothesis_to_prompt(h)
        assert "H001" in prompt
        assert "CONFIRMED" in prompt and "REJECTED" in prompt
        assert "UNCERTAIN" in prompt
        assert "ONLY this hypothesis" in prompt  # 边界约束
        assert "confidence" not in prompt.lower().replace("confidence scores", "")

    def test_prompt_bundled_format(self):
        hyps = [Hypothesis(f"H{i:03d}", f"Candidate {i}", "a.py:1",
                            "b", "flow", [], f"GET /{i}") for i in range(3)]
        prompt = hypothesis_to_bundled_prompt(hyps)
        assert "H000" in prompt and "H002" in prompt
        assert "3 security hypotheses" in prompt


class TestScoring:
    def _make_hyps(self):
        return [
            Hypothesis("H001", "A", "a", "a", "f", [], "GET /a",
                       gt_verdict=Verdict.CONFIRMED),
            Hypothesis("H002", "B", "b", "b", "f", [], "GET /b",
                       gt_verdict=Verdict.REJECTED),
            Hypothesis("H003", "C", "c", "c", "f", [], "GET /c",
                       gt_verdict=Verdict.CONFIRMED),
        ]

    def test_hva_perfect(self):
        hyps = self._make_hyps()
        results = [
            VerificationResult("H001", Verdict.CONFIRMED, None, None, None,
                               evidence=["app.py:10 found"]),
            VerificationResult("H002", Verdict.REJECTED, None, None, None,
                               evidence=["app.py:20 safe"]),
            VerificationResult("H003", Verdict.CONFIRMED, None, None, None,
                               evidence=["app.py:30 found"]),
        ]
        s = score_hva(results, hyps)
        assert s["hva"] == 1.0
        assert s["hcr"] == 1.0
        assert s["egr"] == 1.0

    def test_hva_with_uncertain(self):
        hyps = self._make_hyps()
        results = [
            VerificationResult("H001", Verdict.CONFIRMED, None, None, None,
                               evidence=["app.py:10"]),
            VerificationResult("H002", Verdict.UNCERTAIN, None, None, None),
            VerificationResult("H003", Verdict.REJECTED, None, None, None,
                               evidence=[]),
        ]
        s = score_hva(results, hyps)
        assert s["hcr"] == round(2 / 3, 4)   # H002 未完成
        assert s["hva"] == 0.5                # H001 对, H003 错
        assert s["egr"] == round(1 / 3, 4)    # 只有 H001 有 file:line

    def test_repository_audit_prompt_no_hypothesis(self):
        prompt = repository_audit_prompt("/tmp/app")
        assert "hypothesis" not in prompt.lower()
        assert "audit" in prompt.lower()
