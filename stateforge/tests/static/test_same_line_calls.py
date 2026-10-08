"""H4(评审§14):同行多 call 的排序用 (lineno, col_offset);嵌套调用记限制。"""
from pathlib import Path

from stateforge.analysis.static.icfg import build_icfg

SAME_LINE = '''\
def h(u):
    r = foo(u) + bar(u)
    return r

def foo(x):
    return x

def bar(y):
    return y
'''

NESTED = '''\
def h(u):
    return foo(bar(u))

def foo(x):
    return x

def bar(y):
    return y
'''


def _root(tmp_path, src):
    d = tmp_path / "app"
    d.mkdir()
    (d / "app.py").write_text(src, encoding="utf-8")
    return d


def test_same_line_calls_ordered_by_col(tmp_path):
    ir = build_icfg(_root(tmp_path, SAME_LINE))
    h = ir.functions["app.h"]
    cs = sorted((b for b in h.blocks if ir.blocks[b].kind == "callsite"),
                key=lambda b: int(b.rsplit("::", 1)[-1]))     # 链序 n
    assert len(cs) == 2
    assert ir.outgoing(cs[0], "call")[0].callee == "app.foo"   # col 小者先
    assert ir.outgoing(cs[1], "call")[0].callee == "app.bar"


def test_nested_calls_both_present(tmp_path):
    """foo(bar()):两个 callsite 都在链上;块级顺序是已知限制(真实求值序
    bar→foo,语句级 AST 排序给 foo 先)——见 spec §10。"""
    ir = build_icfg(_root(tmp_path, NESTED))
    cs = [b for b in ir.functions["app.h"].blocks
          if ir.blocks[b].kind == "callsite"]
    assert len(cs) == 2
    callees = {e.callee for e in ir.edges
               if e.src in set(cs) and e.kind == "call"}
    assert callees == {"app.foo", "app.bar"}
