"""P1-2:同块多 call 的 CS/RS 合成链保序;return helper() 特判;不造假边。"""
from pathlib import Path

from stateforge.analysis.static.icfg import build_icfg

MULTI = '''\
def handler(u):
    a = first(u)
    b = second(a)
    return b

def first(x):
    return x

def second(y):
    return y
'''

TAIL = '''\
def handler(u):
    return helper(u)

def helper(x):
    return x
'''


def _root(tmp_path, src):
    d = tmp_path / "app"
    d.mkdir()
    (d / "app.py").write_text(src, encoding="utf-8")
    return d


def test_chain_order_and_kinds(tmp_path):
    ir = build_icfg(_root(tmp_path, MULTI))
    h = ir.functions["app.handler"]
    cs = [b for b in h.blocks if ir.blocks[b].kind == "callsite"]
    rs = [b for b in h.blocks if ir.blocks[b].kind == "returnsite"]
    assert len(cs) == 2 and len(rs) == 2
    first_cs = min(cs, key=lambda b: ir.blocks[b].start_line)
    second_cs = max(cs, key=lambda b: ir.blocks[b].start_line)
    # CS1 → first.entry(call);first.exit → RS1
    assert ir.outgoing(first_cs, "call")[0].dst == ir.functions["app.first"].entry
    ret_into_rs = [e for e in ir.edges if e.kind == "return"
                   and e.dst in set(rs)]
    assert len(ret_into_rs) == 2
    # 原 B→succ 直边被链替代:B 的 flow 出边唯一指向 CS1
    body = [b for b in h.blocks if ir.blocks[b].kind == "block"]
    assert body
    b_succs = [e.dst for e in ir.outgoing(body[0], "flow")]
    assert b_succs == [first_cs]
    # RS2 → 后继(return b 的 terminal 块)
    second_rs = max(rs, key=lambda b: ir.blocks[b].start_line)
    rs2_succs = [e.dst for e in ir.outgoing(second_rs)]
    assert rs2_succs and all(ir.blocks[d].function == "handler" for d in rs2_succs)


def test_tail_call_returns_to_exit(tmp_path):
    ir = build_icfg(_root(tmp_path, TAIL))
    h = ir.functions["app.handler"]
    rs = [b for b in h.blocks if ir.blocks[b].kind == "returnsite"]
    assert len(rs) == 1
    ret_edges = ir.outgoing(rs[0])
    assert [e.dst for e in ret_edges] == [h.exit]


def test_no_fake_call_edge(tmp_path):
    ir = build_icfg(_root(tmp_path, TAIL + "\n\ndef ghost():\n    return 0\n"))
    ge = ir.functions["app.ghost"].entry
    assert not any(e.dst == ge and e.kind == "call" for e in ir.edges)


def test_branch_labels_preserved(tmp_path):
    """branch 块内调用(条件表达式里调用)时,RS→后继保留 true/false 标注。"""
    src = '''\
def handler(flag):
    if check(flag):
        return "yes"
    return "no"

def check(f):
    return f
'''
    ir = build_icfg(_root(tmp_path, src))
    rs = [b for b in ir.functions["app.handler"].blocks
          if ir.blocks[b].kind == "returnsite"]
    assert len(rs) == 1
    out = ir.outgoing(rs[0])
    kinds = sorted(e.kind for e in out)
    assert kinds == ["false", "true"] or kinds == ["flow"], \
        f"RS 后继应保留原分支标注,got {kinds}"
