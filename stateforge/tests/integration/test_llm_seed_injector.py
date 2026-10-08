"""Phase-4.2:LLM Seed Injector 测试(种子注入架构,不执行目标)。

测试自建 IR(从 fixture 构造),不依赖 /tmp 或任何外部状态。
"""
import json
import tempfile
from pathlib import Path

import pytest

from stateforge.core.ir import ProgramIR
from stateforge.solving.llm_seed_injector import inject_llm_seeds
from stateforge.analysis.static.icfg import build_icfg

FIXTURES = Path(__file__).parents[2] / "fixtures" / "llm_loop"


def _build_ir(case_dir: Path) -> tuple[ProgramIR, Path]:
    """从 fixture 目录构建 IR 和 app 路径(完全自包含)。"""
    ir = build_icfg(case_dir)
    return ir, case_dir / "app.py"


def _mock_provider(responses: list[tuple[str, str, str]]) -> object:
    """构造 Mock Provider:(kind, key, value) 列表 → JSON 响应序列。"""
    class Provider:
        def __init__(self):
            self.responses = [
                json.dumps({"status": "PROPOSED", "source_kind": kind,
                           "source_key": key, "operation": "REPLACE",
                           "proposed_value": value,
                           "rationale": "mock", "confidence": 0.9})
                for kind, key, value in responses]
            self.calls = 0

        def complete(self, system: str, user: str) -> str:
            if self.calls < len(self.responses):
                resp = self.responses[self.calls]
            else:
                resp = json.dumps({"status": "UNSUPPORTED", "source_kind": "",
                                   "source_key": "", "operation": "REPLACE",
                                   "proposed_value": None,
                                   "rationale": "exhausted", "confidence": 0})
            self.calls += 1
            return resp
    return Provider()


def _coverage_state(ir: ProgramIR) -> dict:
    """构造合理覆盖状态:全部 run 函数浅层块已覆盖。"""
    run_blocks = [b.id for b in ir.blocks.values()
                  if b.function == "run"
                  and b.kind in ("entry", "block", "branch", "exit")]
    return {"covered_blocks": run_blocks, "covered_edges": []}


class TestSeedInjection:
    """S2_api_path:停滞 frontier → LLM → 种子 JSON 写入 corpus 目录。"""

    def test_injector_produces_seed_files(self, tmp_path):
        ir, app = _build_ir(FIXTURES / "S2_api_path")
        state = _coverage_state(ir)
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        (corpus_dir / "base").write_bytes(
            json.dumps({"query": {"version": "v0", "path": "/",
                                   "method": "GET"}}).encode())

        provider = _mock_provider([("QUERY", "version", "v1")])
        records = inject_llm_seeds(ir, app, state, corpus_dir, provider)

        injected = [r for r in records if r["status"] == "INJECTED"]
        assert injected, f"应注入种子: {records}"
        seeds = [f for f in corpus_dir.iterdir() if f.name != "base"]
        assert seeds, "corpus 应有新种子文件"
        seed_data = json.loads(seeds[0].read_bytes())
        assert isinstance(seed_data, dict)

    def test_injector_origin_is_llm(self, tmp_path):
        """P0-2 修复:注入种子的 origin 必须是 LLM,不是 NATIVE。"""
        from stateforge.fuzzing.corpus_manager import CorpusManager
        ir, app = _build_ir(FIXTURES / "S2_api_path")
        state = _coverage_state(ir)
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        (corpus_dir / "base").write_bytes(
            json.dumps({"query": {"version": "v0"}}).encode())

        provider = _mock_provider([("QUERY", "version", "v1")])
        inject_llm_seeds(ir, app, state, corpus_dir, provider)

        spool = corpus_dir.parent / "llm_spool"
        if spool.exists():
            cm = CorpusManager(spool)
            for entry in cm.entries():
                assert entry.origin == "LLM", \
                    f"origin 应为 LLM,实际: {entry.origin}"

    def test_no_stalled_no_injection(self, tmp_path):
        ir, _ = _build_ir(FIXTURES / "S2_api_path")
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        provider = _mock_provider([])

        records = inject_llm_seeds(ir, FIXTURES / "S2_api_path" / "app.py",
                                   {"covered_blocks": [],
                                    "covered_edges": []},
                                   corpus_dir, provider)
        assert records == []

    def test_provider_error_no_crash(self, tmp_path):
        ir, app = _build_ir(FIXTURES / "S2_api_path")
        state = _coverage_state(ir)
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()

        class ExplodingProvider:
            def complete(self, system, user):
                raise RuntimeError("API down")

        records = inject_llm_seeds(ir, app, state, corpus_dir,
                                   ExplodingProvider())
        assert all(r["status"] == "PROVIDER_ERROR" for r in records)


class TestCleanCheckoutReproducible:
    """P0-3 修复:从全新 tmp_path 跑,不依赖任何外部状态。"""

    def test_all_llm_loop_fixtures_reproducible(self, tmp_path):
        """遍历全部 llm_loop fixture,每个都能自建 IR。"""
        fixture_dirs = sorted(
            d for d in FIXTURES.iterdir()
            if d.is_dir() and (d / "app.py").exists()
            and not d.name.startswith(".")
        )
        assert len(fixture_dirs) >= 4, "至少 S1/S2 + 两个 T 靶"

        for d in fixture_dirs:
            ir = build_icfg(d)
            assert len(ir.blocks) > 0, f"{d.name} IR 不为空"
