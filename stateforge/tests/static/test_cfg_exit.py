"""H1(P0 评审§11):所有 return terminal 必须显式连 function.exit。"""
import ast

from stateforge.analysis.static.cfg import build_function_cfg


def _build(src):
    fdef = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef))
    return build_function_cfg(fdef, "/t/app.py", "app")


def test_early_returns_reach_exit():
    cfg = _build("def foo(x):\n    if x:\n        return 1\n    return 2\n")
    exit_bid = next(b.id for b in cfg.blocks.values() if b.kind == "exit")
    inc = [e for e in cfg.edges if e.dst == exit_bid]
    rets = [b for b in cfg.blocks.values() if b.kind == "terminal"]
    assert len(rets) == 2
    assert len(inc) >= 2, f"exit 应有 ≥2 入边(两个 return),got {len(inc)}"
    src_kinds = {cfg.blocks[e.src].kind for e in inc}
    assert src_kinds == {"terminal"}


def test_fallthrough_and_return_both_reach_exit():
    cfg = _build("def foo(x):\n    if x:\n        return 1\n    y = 2\n")
    exit_bid = next(b.id for b in cfg.blocks.values() if b.kind == "exit")
    inc = [cfg.blocks[e.src].kind for e in cfg.edges if e.dst == exit_bid]
    assert "terminal" in inc and "block" in inc   # return + 自然结尾都到 exit
