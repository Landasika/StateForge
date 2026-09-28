"""Phase-3.1 单元:tracker 版本化 / oracle 距离与 UNREACHABLE / 评分与保留语义。"""
from stateforge.core.ir import ProgramIR, BasicBlock, FunctionIR, FlowEdge
from stateforge.dynamic.mapper import MappedTrace
from stateforge.feedback.frontier_feedback import FrontierDirectedFeedback
from stateforge.feedback.frontier_tracker import FrontierTracker
from stateforge.feedback.distance_oracle import DistanceOracle


def _ir():
    ir = ProgramIR()
    f = "/t/a.py"
    for bid, lo, hi in [("b1", 1, 1), ("b2", 2, 2), ("b3", 3, 3),
                        ("gate", 4, 4), ("deep", 5, 5), ("iso", 7, 7)]:
        ir.blocks[bid] = BasicBlock(bid, f, "app", "h", lo, hi, "block", [],
                                    [lo])
    ir.functions["app.h"] = FunctionIR(
        "app.h", f, "app", "b1", "b3", ["b1", "b2", "b3", "gate", "deep"])
    for s, d in [("b1", "b2"), ("b2", "b3"), ("b3", "gate"), ("gate", "deep")]:
        ir.edges.append(FlowEdge(s, d, "flow"))
    # iso:无边孤岛(真 UNREACHABLE,勿加自环——否则自成前沿)
    return ir


def _trace(blocks, backed_pairs=()):
    from collections import Counter
    mt = MappedTrace()
    mt.block_hits = Counter(blocks)
    mt.edge_hits = Counter(backed_pairs)
    mt.edge_kinds = {p: {"flow"} for p in backed_pairs}
    return mt


def test_tracker_versions_on_growth_only():
    tr = FrontierTracker(_ir())
    assert tr.update({"b1", "b2", "b3"}, {("b1", "b2"), ("b2", "b3")}) is True
    v1 = tr.version
    assert tr.update({"b1", "b2", "b3"}, {("b1", "b2"), ("b2", "b3")}) is False \
        and tr.version == v1
    assert ("b3", "gate") in tr.edge_frontiers            # 源块已覆盖、边未走


def test_oracle_distance_and_unreachable():
    ir = _ir()
    o = DistanceOracle(ir)
    o.rebuild([("b3", "gate")], version=1)
    assert o.distance({"b3"})[0] == 0                  # 锚点=未走边的源块
    assert o.distance({"b2"})[0] == 1
    assert o.distance({"b1"})[0] == 2
    assert o.distance({"gate"})[0] is None             # 前沿之后:接近度未定义
    assert o.distance({"iso"})[0] is None              # UNREACHABLE


def test_proximity_seed_retained_without_novelty():
    fb = FrontierDirectedFeedback(_ir())
    # 先覆盖 b1→b2→b3(前沿变为 b3→gate)
    fb.commit(_trace(["b1", "b2", "b3"], [("b1", "b2"), ("b2", "b3")]), True)
    # 到达 b3 的重复输入:NB=NE=0,但 D=0 → 应以 frontier_proximity 保留
    obs = fb.observe(_trace(["b3"], []))
    assert obs.new_blocks == 0 and obs.distance == 0 and obs.proximity == 1.0
    assert obs.interesting and obs.reason == "frontier_proximity"
    assert abs(obs.score - 1.0) < 1e-9


def test_score_shape_and_unreachable_zero_proximity():
    fb = FrontierDirectedFeedback(_ir())
    fb.commit(_trace(["b1", "b2"], [("b1", "b2")]), True)
    fb.commit(_trace(["iso"], []), False)               # 孤岛预先覆盖(不再产生 NB)
    obs = fb.observe(_trace(["iso"], []))
    assert obs.distance is None and obs.proximity == 0.0
    assert not obs.interesting


def test_state_roundtrip_across_instances():
    fb = FrontierDirectedFeedback(_ir())
    fb.commit(_trace(["b1", "b2", "b3"], [("b1", "b2"), ("b2", "b3")]), True)
    state = fb.export_state()
    fb2 = FrontierDirectedFeedback(_ir())
    fb2.load_state(state)
    assert ("b3", "gate") in fb2.tracker.edge_frontiers
    obs = fb2.observe(_trace(["b3"], []))
    assert obs.distance == 0 and obs.reason == "frontier_proximity"


def test_frontier_version_retires_champion():
    fb = FrontierDirectedFeedback(_ir())
    fb.commit(_trace(["b1", "b2"], [("b1", "b2")]), True)
    # b2 冠军 D=0(前沿 b2→b3)
    fb.commit(_trace(["b2"], []), True)
    assert fb.best_distance == 0
    # 覆盖增长推进前沿 → 冠军退役
    fb.commit(_trace(["b1", "b2", "b3"], [("b1", "b2"), ("b2", "b3")]), True)
    assert fb.best_distance is None
