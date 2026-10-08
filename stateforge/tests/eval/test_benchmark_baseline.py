"""OWASP Benchmark for Python 基线验证(Phase-1 结构管线 on 1236 文件靶场)。

克隆不在位时整体跳过(外部依赖 ~/icassp/external/BenchmarkPython)。
"""
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3] / "external" / "BenchmarkPython"

pytestmark = pytest.mark.skipif(not REPO.exists(),
                                reason="BenchmarkPython clone absent")


def test_benchmark_baseline(tmp_path):
    from stateforge.evaluation.benchmark_baseline import run_baseline

    payload = run_baseline(REPO, sample=12, out=tmp_path)
    # 静态:真实规模 + call/return 严格配对
    assert payload["static"]["functions"] > 1000
    assert payload["static"]["blocks"] > 10000
    assert payload["static"]["call_edges"] == payload["static"]["return_edges"]
    # 动态:采样请求全部落轨,覆盖/前沿非空,request 维度计数正确
    assert payload["dynamic"]["requests"] == 12
    assert payload["dynamic"]["covered_blocks"] > 50
    assert payload["frontiers"]["total"] > 0
    assert payload["coverage"]["block_ratio"] > 0
    assert (tmp_path / "coverage.dot").exists()
    assert (tmp_path / "trace.jsonl").exists()
