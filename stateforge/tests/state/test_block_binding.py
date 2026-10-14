"""诊断 3 回归:StateFactCandidate 锚定 CFG 基本块(block_id)。

extract_state_facts(source_dir, ir=...) 在给 ProgramIR 时,每条事实按
(file, lineno) → 命中行距区间的最窄基本块;ir 缺省时 block_id=None
(行为与旧版一致,契约兼容)。
"""

from pathlib import Path

from stateforge.analysis.static.icfg import build_icfg
from stateforge.state.extractor import extract_state_facts

_APP = '''
session = {}


def handler(x):
    if session.get("role"):
        return "admin-path"
    session["seen"] = "yes"
    return "anon-path"
'''


def _setup(tmp_path: Path):
    (tmp_path / "app.py").write_text(_APP, encoding="utf-8")
    ir = build_icfg(tmp_path)
    return extract_state_facts(tmp_path, ir=ir), ir


def test_facts_bind_to_block_ids(tmp_path):
    facts, ir = _setup(tmp_path)
    assert facts, "fixture should yield state facts"
    bound = [f for f in facts if f.block_id is not None]
    assert bound, "with ir given, facts should carry block_id"
    # 绑定的 block_id 必须真实存在于 IR
    for f in bound:
        assert f.block_id in ir.blocks
        blk = ir.blocks[f.block_id]
        assert blk.start_line <= f.lineno <= blk.end_line


def test_guard_read_binds_to_its_branch_block(tmp_path):
    facts, _ = _setup(tmp_path)
    guard_read = next(f for f in facts
                      if f.object_ref == "session.role"
                      and f.relevance.value == "CONTROL")
    assert guard_read.block_id is not None


def test_without_ir_block_id_stays_none(tmp_path):
    (tmp_path / "app.py").write_text(_APP, encoding="utf-8")
    facts = extract_state_facts(tmp_path)
    assert facts
    assert all(f.block_id is None for f in facts)


def test_out_of_range_lineno_returns_none(tmp_path):
    """行号不在任何块区间(空 IR/异源 IR)→ None,不崩。"""
    (tmp_path / "app.py").write_text(_APP, encoding="utf-8")
    ir = build_icfg(tmp_path)
    from stateforge.state.extractor import _BlockIndex
    idx = _BlockIndex(ir)
    assert idx.lookup(str(tmp_path / "app.py"), 10_000) is None
    assert idx.lookup(str(tmp_path / "nope.py"), 1) is None
