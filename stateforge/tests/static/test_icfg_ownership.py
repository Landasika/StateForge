"""P1-1:嵌套函数/lambda 内的调用不得归属到外层函数的块。"""
import ast
from pathlib import Path

from stateforge.analysis.static.icfg import _iter_owned_calls, build_icfg

SRC = '''\
def outer(x):
    helper(x)
    def inner(y):
        helper(y)
        return other(y)
    f = lambda z: helper(z)
    return inner(x)

def helper(v):
    return v
'''


def _tmp(tmp_path: Path, src: str) -> Path:
    d = tmp_path / "app"
    d.mkdir()
    (d / "app.py").write_text(src, encoding="utf-8")
    return d


def test_nested_def_calls_not_owned():
    tree = ast.parse(SRC)
    outer = tree.body[0]
    names = [c.func.id for c in _iter_owned_calls(outer)
             if isinstance(c.func, ast.Name)]
    # outer 直接调用 helper 与 inner;inner 体里的 helper/other、lambda 里的 helper 不算
    assert sorted(names) == ["helper", "inner"]
    lines = [c.lineno for c in _iter_owned_calls(outer)]
    assert set(lines) == {2, 7}          # 不含 inner 体(4/5)与 lambda 行(6)


def test_icfg_nested_call_not_in_outer_blocks(tmp_path):
    root = _tmp(tmp_path, SRC)
    ir = build_icfg(root)
    assert "app.outer" in ir.functions
    assert "app.inner" in ir.functions
    call_edges = [e for e in ir.edges if e.kind == "call"
                  and e.src in set(ir.functions["app.outer"].blocks)]
    # outer 的调用边只落在自己的行(2:helper、6:inner),不含 inner 体(4/5)
    assert {e.callsite_line for e in call_edges} == {2, 7}
