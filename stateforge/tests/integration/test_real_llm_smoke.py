"""Phase-4.2-F:真实 LLM smoke test(默认跳过,需 STATEFORGE_RUN_REAL_LLM_TESTS=1)。"""
import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("STATEFORGE_RUN_REAL_LLM_TESTS") != "1",
    reason="需 STATEFORGE_RUN_REAL_LLM_TESTS=1 且 STATEFORGE_LLM_API_KEY 已配置")

FIXTURES = Path(__file__).parents[2] / "fixtures" / "llm_loop"


def test_real_llm_t1_smoke(tmp_path):
    """T1 magic string:真实 API → proposal → validator → candidate → 执行 → crossed。"""
    from stateforge.feasibility.coverage_loop import LLMCoverageLoop
    from stateforge.llm.real_provider import RealLLMProvider

    d = FIXTURES / "T1_magic_string"
    seed = json.loads((d / "input.json").read_text())
    provider = RealLLMProvider()
    assert provider.configured, "STATEFORGE_LLM_API_KEY not set"

    loop = LLMCoverageLoop(app_path=d / "app.py", seed=seed,
                           provider=provider, workdir=tmp_path,
                           max_attempts_per_frontier=3, max_total_calls=5)
    result = loop.run()
    # 不强求 target_reached(模型可能不配合);但全链不应 crash
    assert result.attempts, "至少有一次 LLM 调用"
    for a in result.attempts:
        assert a.failure_reason != "PROVIDER_ERROR", a.failure_reason
    print(f"invoked={sum(a.invoked for a in result.attempts)} "
          f"crossed={sum(a.frontier_crossed for a in result.attempts)} "
          f"target_reached={result.target_reached}")
