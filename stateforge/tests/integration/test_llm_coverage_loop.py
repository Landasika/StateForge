"""Phase-4.2-E:LLM-Guided Coverage Loop E2E integration test.

顾问 §十七 Task 2:先写红测试——极小 target + Mock LLM,要求完整走通
stall → slice → mock LLM → validate → execute → new coverage → corpus admission。
"""
import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parents[2] / "fixtures" / "llm_loop"


class MockProvider:
    """可控 mock:返回预设 LLM 输出。"""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        if self.calls < len(self.responses):
            resp = self.responses[self.calls]
        else:
            resp = '{"status":"UNSUPPORTED"}'
        self.calls += 1
        return resp


def _proposal(source, key, value, status="PROPOSED"):
    return json.dumps({"status": status, "source_kind": source,
                       "source_key": key, "operation": "REPLACE",
                       "proposed_value": value,
                       "rationale": "mock", "confidence": 0.9})


class TestLLMCoverageLoopE2E:
    """顾问 DoD 全链:T1 magic string 为代表。"""

    def test_t1_magic_string_full_loop(self, tmp_path):
        """从 seed=AAAA 到 hidden():LLM 产 MAGIC_STATEFORGE → corpus admission。"""
        from stateforge.feasibility.coverage_loop import LLMCoverageLoop

        d = FIXTURES / "T1_magic_string"
        seed = json.loads((d / "input.json").read_text())
        # LLM 第一次:接近但不够;第二次:正确
        provider = MockProvider([
            _proposal("QUERY", "payload", "MAGIC"),
            _proposal("QUERY", "payload", "MAGIC_STATEFORGE"),
        ])
        loop = LLMCoverageLoop(app_path=d / "app.py", seed=seed,
                               provider=provider, workdir=tmp_path)
        result = loop.run()

        # 核心断言:真实执行后 hidden() 被覆盖(mapper 裁决)
        assert result.target_reached, (
            f"hidden() 应被覆盖;attempts={result.attempts}")
        # LLM 被调了 2 次(第一次 MAGIC 只过第一道门)
        assert provider.calls >= 2
        # 候选进了 corpus(可被后续 fuzz 变异)
        assert result.corpus_admitted, "有效候选应进 corpus"
        # 每次调用有结构化记录
        assert len(result.attempt_records) == provider.calls

    def test_t2_numeric_threshold(self, tmp_path):
        from stateforge.feasibility.coverage_loop import LLMCoverageLoop
        d = FIXTURES / "T2_numeric"
        seed = json.loads((d / "input.json").read_text())
        provider = MockProvider([_proposal("QUERY", "n", 999)])
        loop = LLMCoverageLoop(app_path=d / "app.py", seed=seed,
                               provider=provider, workdir=tmp_path)
        result = loop.run()
        assert result.target_reached

    def test_t6_semantic_two_fields(self, tmp_path):
        from stateforge.feasibility.coverage_loop import LLMCoverageLoop
        d = FIXTURES / "T6_semantic"
        seed = json.loads((d / "input.json").read_text())
        # 需两步:先 admin 再 delete(或一步双改——第一版单 mutation 则两轮)
        provider = MockProvider([
            _proposal("QUERY", "username", "administrator"),
            _proposal("QUERY", "action", "delete"),
        ])
        loop = LLMCoverageLoop(app_path=d / "app.py", seed=seed,
                               provider=provider, workdir=tmp_path)
        result = loop.run()
        assert result.target_reached, f"两步语义推理应到达 hidden"

    def test_llm_exhausted_after_k_attempts(self, tmp_path):
        """K 次全失败 → LLM_EXHAUSTED → 不再调(预算控制)。"""
        from stateforge.feasibility.coverage_loop import LLMCoverageLoop
        d = FIXTURES / "T1_magic_string"
        seed = json.loads((d / "input.json").read_text())
        # K=3;全部返回垃圾
        provider = MockProvider(["garbage"] * 10)
        loop = LLMCoverageLoop(app_path=d / "app.py", seed=seed,
                               provider=provider, workdir=tmp_path,
                               max_attempts_per_frontier=3)
        result = loop.run()
        assert not result.target_reached
        assert provider.calls == 3, "K=3 后应停止"
