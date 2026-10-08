"""F1(封版轮):raise 语句终止控制流,不顺序落到后续语句。"""
import ast

from stateforge.analysis.static.cfg import build_function_cfg


def _build(src):
    fdef = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef))
    return build_function_cfg(fdef, "/t/a.py", "a")


def test_raise_does_not_fall_through():
    cfg = _build("def f(x):\n    if x:\n        raise ValueError(x)\n    return 1\n")
    raise_blocks = [b for b in cfg.blocks.values()
                    if any(s.startswith("raise") for s in b.statements)]
    assert raise_blocks, "raise 语句应成 terminal 块"
    succ = [cfg.blocks[e.dst].kind for e in cfg.edges if e.src == raise_blocks[0].id]
    assert succ == ["exit"], f"raise 应直连函数出口,got {succ}"
    # 后续 return 1 只能从 false 分支到达,不能从 raise 顺序落下
    false_targets = [cfg.blocks[e.dst].start_line for e in cfg.edges
                     if e.src and cfg.blocks[e.src].kind == "branch" and e.kind == "false"]
    assert false_targets == [4]


