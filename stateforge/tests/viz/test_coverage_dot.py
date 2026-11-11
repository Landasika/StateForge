"""P1-5:viz 冒烟——dot 生成、覆盖着色、frontier 高亮。"""
from stateforge.core.ir import ProgramIR, BasicBlock, FunctionIR, FlowEdge
from stateforge.evaluation.viz.coverage_dot import coverage_dot


def _ir():
    ir = ProgramIR()
    f = "/t/app.py"
    ir.blocks["b1"] = BasicBlock("b1", f, "app", "h", 1, 2, "block", [], [1, 2])
    ir.blocks["b2"] = BasicBlock("b2", f, "app", "h", 3, 4, "block", [], [3, 4])
    ir.functions["app.h"] = FunctionIR("app.h", f, "app", "b1", "b2", ["b1", "b2"])
    ir.edges.append(FlowEdge("b1", "b2", "true"))
    return ir


def test_dot_smoke(tmp_path):
    fb = {"covered_blocks": ["b1"],
          "frontiers": [{"src": "b1", "dst": "b2", "kind": "true"}],
          "block_hits": {"b1": 3}}
    out = coverage_dot(_ir(), fb, tmp_path / "cov.dot")
    text = out.read_text(encoding="utf-8")
    assert text.startswith("digraph")
    assert 'fillcolor="palegreen"' in text      # 覆盖块着色
    assert 'color="gray"' in text               # 未覆盖块灰
    assert "red" in text and "frontier" in text  # frontier 边红


def test_edge_attributes_inside_single_bracket_group():
    import re

    from stateforge.core.ir import FlowEdge

    ir = _ir()
    ir.edges.append(FlowEdge("b2", "b1", "call", callee="x"))
    fb = {"covered_blocks": [], "frontiers": [], "block_hits": {}}
    text = coverage_dot(ir, fb, "/tmp/_shape.dot").read_text(encoding="utf-8")
    for line in text.splitlines():
        if "->" in line:
            assert re.match(r'^  ".*" -> ".*"( \[.*\])?;$', line), \
                f"边属性必须在单一方括号组内: {line}"


def test_session_block_marked_even_when_uncovered():
    """session 标记不能因未覆盖而丢失——「状态变了但没跑到」正是要找的块。"""
    ir = _ir()
    fb = {"covered_blocks": [], "frontiers": [], "block_hits": {}}
    text = coverage_dot(ir, fb, "/tmp/_sess.dot",
                        session_blocks=["b2"]).read_text(encoding="utf-8")
    b2 = [l for l in text.splitlines() if '"b2" [' in l][0]
    assert 'color="darkorange"' in b2
    assert "penwidth=2.5" in b2
    assert 'fontcolor="gray"' in b2   # 仍然是未覆盖
    b1 = [l for l in text.splitlines() if '"b1" [' in l][0]
    assert "darkorange" not in b1


def test_learned_edge_marked_dashed_blue():
    ir = _ir()
    fb = {"covered_blocks": ["b1", "b2"], "frontiers": [], "block_hits": {}}
    text = coverage_dot(ir, fb, "/tmp/_dyn.dot",
                        learned_edges=[("b1", "b2")]).read_text(encoding="utf-8")
    edge = [l for l in text.splitlines() if "b1\" -> \"b2\"" in l][0]
    assert 'color="blue"' in edge and "style=dashed" in edge
    assert 'label="learned"' in edge


def test_function_filter_keeps_only_that_function():
    ir = _ir()
    ir.blocks["b3"] = BasicBlock("b3", "/t/app.py", "app", "other", 5, 6,
                                 "block", [], [5, 6])
    fb = {"covered_blocks": [], "frontiers": [], "block_hits": {}}
    text = coverage_dot(ir, fb, "/tmp/_fn.dot",
                        function="h").read_text(encoding="utf-8")
    assert '"b1" [' in text and '"b2" [' in text
    assert '"b3" [' not in text


def test_around_keeps_neighbours():
    ir = _ir()
    ir.blocks["b3"] = BasicBlock("b3", "/t/app.py", "app", "h", 5, 6,
                                 "block", [], [5, 6])
    fb = {"covered_blocks": [], "frontiers": [], "block_hits": {}}
    text = coverage_dot(ir, fb, "/tmp/_around.dot",
                        around="b1").read_text(encoding="utf-8")
    assert '"b1" [' in text and '"b2" [' in text   # 直接邻居
    assert '"b3" [' not in text                     # 无关块


def test_only_learned_draws_no_static_edges():
    ir = _ir()
    ir.edges.append(FlowEdge("b2", "b1", "flow"))
    fb = {"covered_blocks": [], "frontiers": [], "block_hits": {}}
    text = coverage_dot(ir, fb, "/tmp/_only.dot",
                        learned_edges=[("b1", "b2")],
                        only_learned=True).read_text(encoding="utf-8")
    edges = [l for l in text.splitlines() if "->" in l]
    assert len(edges) == 1, f"只该剩 1 条动态边: {edges}"
    assert "learned" in edges[0]


def test_empty_filter_is_an_error():
    import pytest
    fn = lambda: coverage_dot(_ir(), {"covered_blocks": []}, "/tmp/_x.dot",
                              function="no_such_function")
    with pytest.raises(ValueError, match="no blocks matched"):
        fn()
