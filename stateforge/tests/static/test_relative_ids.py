"""H6(评审§2):块 ID 用相对路径,跨机器稳定;file 字段仍绝对。"""
from pathlib import Path

from stateforge.static.icfg import build_icfg

SRC = "def h(u):\n    return helper(u)\n\n\ndef helper(x):\n    return x\n"


def test_ids_relative_file_absolute(tmp_path):
    d = tmp_path / "app"
    d.mkdir()
    (d / "app.py").write_text(SRC, encoding="utf-8")
    ir = build_icfg(d)
    for bid, block in ir.blocks.items():
        assert str(d) not in bid, f"ID 含绝对路径: {bid}"
        assert block.file.startswith(str(d)), "file 字段应保持绝对路径供匹配"
    cs = [b for b in ir.blocks.values() if b.kind == "callsite"]
    assert cs and all(str(d) not in b.id for b in cs)
