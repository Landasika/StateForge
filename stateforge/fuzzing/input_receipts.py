"""Optional exact-input receipts written inside the Atheris callback."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from stateforge.core.seeds import decode_sequence_seed


class InputReceiptWriter:
    def __init__(self, path: str | Path, *, target_block: str,
                 run_id: str = "", expected_sha256: str | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.target_block = target_block
        self.run_id = run_id
        self.expected_sha256 = expected_sha256

    def record(self, data: bytes, execution, mapped, *, started_steps: int,
               completed_steps: int) -> None:
        digest = hashlib.sha256(data).hexdigest()
        if self.expected_sha256 is not None and digest != self.expected_sha256:
            return
        steps = decode_sequence_seed(data)
        result = execution.result if execution is not None else None
        error = (f"{type(execution.error).__name__}: {execution.error}"
                 if execution is not None and execution.error is not None else None)
        receipt = {
            "engine": "atheris", "phase": "input_callback", "pid": os.getpid(),
            "run_id": self.run_id, "sha256": digest, "size": len(data),
            "sequence_steps": len(steps) if steps is not None else None,
            "started_steps": started_steps, "completed_steps": completed_steps,
            "lifecycle": result.lifecycle if result is not None else None,
            "isolation": result.isolation if result is not None else None,
            "target_block": self.target_block,
            "target_covered": bool(mapped is not None and self.target_block in mapped.blocks),
            "execution_recorded": execution is not None, "error": error,
        }
        # Each receipt survives libFuzzer's _Exit; no finalizer is required.
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(receipt, ensure_ascii=False) + "\n")
            stream.flush()
