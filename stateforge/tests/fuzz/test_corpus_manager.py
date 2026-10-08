"""Phase-2 模块 2:CorpusManager——去重/持久化/导出。"""
from stateforge.fuzzing.corpus_manager import CorpusManager


def test_save_dedup_and_roundtrip(tmp_path):
    cm = CorpusManager(tmp_path / "spool")
    cid = cm.save(b"\x01\x02", epoch=1, new_blocks=2, new_edges=1,
                  discovered_blocks=5, discovered_edges=3)
    assert cid
    assert cm.save(b"\x01\x02", epoch=2, new_blocks=9, new_edges=9,
                   discovered_blocks=9, discovered_edges=9) is None  # 去重
    # 原始字节可回放
    assert (cm.seeds_dir / cid).read_bytes() == b"\x01\x02"
    meta = cm.entries()
    assert len(meta) == 1 and meta[0].structural_score == 3


def test_export_seed_dir_merges_extra(tmp_path):
    cm = CorpusManager(tmp_path / "spool")
    cm.save(b"seedA", 1, 1, 0, 2, 1)
    lf = tmp_path / "lf_corp"
    lf.mkdir()
    (lf / "lf1").write_bytes(b"lfx")
    out = tmp_path / "next"
    n = cm.export_seed_dir(out, extra=[lf])
    files = sorted(p.read_bytes() for p in out.iterdir())
    assert n == 2 and files == [b"lfx", b"seedA"]


def test_persists_across_instances(tmp_path):
    CorpusManager(tmp_path / "spool").save(b"abc", 1, 1, 1, 1, 1)
    again = CorpusManager(tmp_path / "spool")
    assert again.save(b"abc", 2, 5, 5, 5, 5) is None    # 重启后仍去重
