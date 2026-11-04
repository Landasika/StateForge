"""guidance 测试共用的靶场装载与距离阶梯断言工具。

靶场是 ``stateforge/targets/guidance_m1``(纯 Python,无外部依赖):
``target(data)`` 要求 ``A``→``B``→``C`` 三道门才到达 ``target_block``,
距离阶梯 3→2→1→0 单调,故"引导是否在收敛"可被断言。
"""

from __future__ import annotations

from pathlib import Path

from stateforge.adapters.callable import CallableAdapter
from stateforge.analysis.static.icfg import build_icfg
from stateforge.core.ir import ProgramIR

TARGET_ROOT = Path(__file__).resolve().parents[2] / "targets" / "guidance_m1"


def build_target_ir() -> ProgramIR:
    return build_icfg(TARGET_ROOT)


def target_block_id(ir: ProgramIR) -> str:
    """目标块的 entry(``target_block`` 的第一块)。"""
    return ir.functions["target.target_block"].entry


def target_call_terminal(ir: ProgramIR) -> str:
    """``return target_block()`` 所在块——离目标最近的**本函数**块。"""
    for block_id, block in ir.blocks.items():
        if block.function == "target" and any(
                "target_block()" in statement for statement in block.statements):
            return block_id
    raise AssertionError("target terminal block not found")


def adapter():
    from stateforge.targets.guidance_m1 import target as module
    return CallableAdapter(module.target)


def seeds() -> list[bytes]:
    return [item.read_bytes()
            for item in sorted((TARGET_ROOT / "seeds").iterdir())
            if item.is_file()]


def replay() -> bytes:
    return (TARGET_ROOT / "replay.bin").read_bytes()
