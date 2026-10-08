"""H2(评审§15,P0):line→块映射精确 line_numbers 优先,range 兜底。"""
from stateforge.core.ir import ProgramIR, BasicBlock, FunctionIR
from stateforge.runtime.mapper import map_trace
from stateforge.runtime.tracer import TraceEvent


def _ir():
    ir = ProgramIR()
    f = "/t/app.py"
    ir.blocks["outer"] = BasicBlock("outer", f, "app", "h", 10, 20, "block",
                                    [], [10, 11, 20])
    ir.blocks["inner"] = BasicBlock("inner", f, "app", "h", 15, 15, "branch",
                                    [], [15])
    ir.functions["app.h"] = FunctionIR("app.h", f, "app", "outer", "outer",
                                       ["outer", "inner"])
    return ir


def test_nested_line_maps_to_exact_block():
    mt = map_trace([TraceEvent(0, 0, "/t/app.py", "h", 15, "line")], _ir())
    assert mt.blocks == ["inner"], "line 15 精确属于 branch 块,不得被外块 range 吞"


def test_multiline_stmt_falls_back_to_range():
    # 行 13 是某多行语句的中段:不在任何 line_numbers,range 兜底命中 outer
    mt = map_trace([TraceEvent(0, 0, "/t/app.py", "h", 13, "line")], _ir())
    assert mt.blocks == ["outer"]


def test_synthetic_tie_prefers_real_block():
    ir = _ir()
    f = "/t/app.py"
    # CS 与宿主语句同行:line 事件应归宿主 block,不归合成 callsite
    ir.blocks["cs"] = BasicBlock("cs", f, "app", "h", 10, 10, "callsite",
                                 [], [10])
    ir.functions["app.h"].blocks.append("cs")
    mt = map_trace([TraceEvent(0, 0, f, "h", 10, "line")], ir)
    assert mt.blocks == ["outer"]
