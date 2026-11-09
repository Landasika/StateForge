"""H3(评审§13,P0/P1):名字解析歧义时用预生成 callgraph(PyCG)消歧。"""
from pathlib import Path

from stateforge.analysis.static.icfg import build_icfg

SRC_A = '''\
from b import get
from c import get            # 同名重绑定:哪个 get 都不确定

def handler(x):
    return get(x)
'''
SRC_B = "def get(x):\n    return x\n"
SRC_C = "def get(x):\n    return x + 1\n"


def _root(tmp_path):
    d = tmp_path / "app"
    d.mkdir()
    (d / "a.py").write_text(SRC_A, encoding="utf-8")
    (d / "b.py").write_text(SRC_B, encoding="utf-8")
    (d / "c.py").write_text(SRC_C, encoding="utf-8")
    return d


def test_imported_name_is_qualified(tmp_path):
    """``from b import get`` 后调用 ``get`` 就是 ``b.get``:导入表给了确定证据。"""
    d = tmp_path / "app"
    d.mkdir()
    (d / "a.py").write_text("from b import get\n\ndef handler(x):\n    return get(x)\n",
                            encoding="utf-8")
    (d / "b.py").write_text(SRC_B, encoding="utf-8")
    (d / "c.py").write_text(SRC_C, encoding="utf-8")
    ir = build_icfg(d)
    assert any(e.callee == "b.get" for e in ir.edges if e.kind == "call")
    assert not any(u["caller"] == "a.handler" for u in ir.metadata["unresolved_calls"])


def test_ambiguous_without_callgraph(tmp_path):
    ir = build_icfg(_root(tmp_path))
    # 两个同名 get:名字解析不出 → unresolved,不造假边
    assert any(u["callee"] == "get" and u["caller"] == "a.handler"
               for u in ir.metadata["unresolved_calls"])
    assert not any(e.callee and e.callee.endswith("get")
                   for e in ir.edges if e.kind == "call")


def test_callgraph_disambiguates(tmp_path):
    cg = {"a.handler": ["b.get"]}
    ir = build_icfg(_root(tmp_path), callgraph=cg)
    call = next(e for e in ir.edges if e.kind == "call" and e.callee == "b.get")
    assert call is not None
    # 配对 return 边存在
    assert any(e.kind == "return" and e.callee == "b.get" for e in ir.edges)


def test_callgraph_suffix_names(tmp_path):
    # PyCG 产物 qname 带仓库前缀(vulnshop.a.handler),后缀匹配要命中
    cg = {"repo.a.handler": ["repo.b.get"]}
    ir = build_icfg(_root(tmp_path), callgraph=cg)
    assert any(e.callee == "b.get" for e in ir.edges if e.kind == "call")
