"""Stable serialization helpers for structural experiment artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from stateforge.core.ir import ProgramIR


def save_program_ir(ir: ProgramIR, path: str | Path) -> None:
    ir.to_json(path)


def load_program_ir(path: str | Path) -> ProgramIR:
    return ProgramIR.from_json(path)


def save_json(data: Any, path: str | Path) -> None:
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def coerce_program_ir(ir: ProgramIR | str | Path | None) -> ProgramIR | None:
    """Load an optional IR artifact; unavailable/invalid artifacts yield None.

    Strict callers should use load_program_ir, which preserves load errors.
    """
    if ir is None or isinstance(ir, ProgramIR):
        return ir
    try:
        return load_program_ir(ir)
    except (OSError, ValueError):
        return None
