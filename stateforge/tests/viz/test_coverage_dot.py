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
