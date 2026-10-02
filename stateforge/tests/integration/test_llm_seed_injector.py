"""Phase-4.2:LLM Seed Injector 测试(种子注入架构,不执行目标)。"""
import json
from pathlib import Path

FIXTURES = Path(__file__).parents[2] / "fixtures" / "llm_loop"


def test_injector_produces_seed_files(tmp_path):
    """S2:停滞 frontier → LLM → 种子 JSON 写入 corpus 目录。"""
    from stateforge.core.ir import ProgramIR
    from stateforge.feasibility.llm_seed_injector import inject_llm_seeds

    d = FIXTURES / "S2_api_path"
    ir = ProgramIR.from_json("/tmp/ir_S2/program_ir.json")

    # 模拟覆盖:全部 run 的浅层块已覆盖(真实执行会到达的)
    run_blocks = [b.id for b in ir.blocks.values()
                  if b.function == "run"
                  and b.kind in ("entry", "block", "branch", "exit")]
    state = {"covered_blocks": run_blocks,
             "covered_edges": []}

    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    (corpus_dir / "base").write_bytes(
        json.dumps({"query": {"version": "v0", "path": "/",
                               "method": "GET"}}).encode())

    # S2 的 provider
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "s2_provider", d / ".llm_provider.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    records = inject_llm_seeds(ir, d, state, corpus_dir, mod.provider)

    # 至少一个种子被注入
    injected = [r for r in records if r["status"] == "INJECTED"]
    assert injected, f"应注入种子: {records}"
    # 种子文件在 corpus 目录中
    seeds = [f for f in corpus_dir.iterdir() if f.name != "base"]
    assert seeds, "corpus 应有新种子文件"
    # 种子内容是有效 JSON
    seed_data = json.loads(seeds[0].read_bytes())
    assert isinstance(seed_data, dict)


def test_injector_no_stalled_no_injection(tmp_path):
    """无停滞 frontier → 不调 LLM。"""
    from stateforge.core.ir import ProgramIR
    from stateforge.feasibility.llm_seed_injector import inject_llm_seeds

    ir = ProgramIR.from_json("/tmp/ir_S2/program_ir.json")
    # 空覆盖 → 无 frontier
    state = {"covered_blocks": [], "covered_edges": []}
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()

    records = inject_llm_seeds(ir, FIXTURES, state, corpus_dir,
                               _NoopProvider())
    assert records == []


def test_injector_provider_error_no_crash(tmp_path):
    """LLM 崩溃 → 记录 PRODIVER_ERROR,不 crash。"""
    from stateforge.core.ir import ProgramIR
    from stateforge.feasibility.llm_seed_injector import inject_llm_seeds

    ir = ProgramIR.from_json("/tmp/ir_S2/program_ir.json")
    branch_id = next(b.id for b in ir.blocks.values()
                     if b.kind == "branch" and b.function == "run")
    state = {"covered_blocks": [branch_id],
             "covered_edges": []}
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()

    records = inject_llm_seeds(ir, FIXTURES, state, corpus_dir,
                               _ExplodingProvider())
    assert all(r["status"] == "PROVIDER_ERROR" for r in records)


class _NoopProvider:
    calls = 0
    def complete(self, system, user):
        self.calls += 1
        return json.dumps({"status": "UNSUPPORTED"})


class _ExplodingProvider:
    def complete(self, system, user):
        raise RuntimeError("API down")
