"""Phase-2 模块 1:StructuralFeedback——先评分后更新;unresolved 边不计。"""
from stateforge.dynamic.mapper import MappedTrace
from stateforge.feedback.structural import StructuralFeedback


def _trace(pairs, blocks):
    from collections import Counter
    mt = MappedTrace()
    mt.block_hits = Counter(blocks)
    mt.edge_hits = Counter(pairs)
    mt.edge_kinds = {p: {"flow"} for p, backed in pairs.items() if backed}
    return mt


def test_score_uses_pre_execution_snapshot():
    fb = StructuralFeedback()
    t1 = _trace({("a", "b"): True}, ["a", "b"])
    ob1 = fb.observe(t1)
    assert ob1.score == 3 and ob1.interesting        # 2 新块 + 1 新边
    fb.update_global_coverage(ob1.blocks, ob1.edges)
    # 同一输入再来:相对更新后的全局,新颖度为 0
    ob2 = fb.observe(t1)
    assert ob2.score == 0 and not ob2.interesting


def test_unbacked_edges_excluded():
    fb = StructuralFeedback()
    t = _trace({("x", "y"): False, ("p", "q"): True}, ["x", "y", "p", "q"])
    ob = fb.observe(t)
    assert ("x", "y") not in ob.edges, "无静态支撑的邻接对不得计入"
    assert ob.edges == {("p", "q")}
    assert ob.score == 4 + 1


def test_partial_novelty_counts_only_delta():
    fb = StructuralFeedback()
    fb.update_global_coverage({"a"}, {("a", "b")})
    t = _trace({("a", "b"): True, ("b", "c"): True}, ["a", "b", "c"])
    ob = fb.observe(t)
    assert ob.new_blocks == {"b", "c"} and ob.new_edges == {("b", "c")}
    assert ob.score == 3
