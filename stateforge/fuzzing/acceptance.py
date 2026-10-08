"""T+6 acceptance driver.  Usage:
    PYTHONPATH=/home/landasika/icassp python -m stateforge.fuzzing.acceptance [args]
libFuzzer args pass through, e.g. `-runs=500` or `-max_total_time=10`.
"""
from __future__ import annotations

import sys

from stateforge.adapters.vulnshop import vulnshop_adapter
from stateforge.fuzzing.atheris_runner import run_atheris

ROOT = "/home/landasika/icassp/stateforge/targets/vulnshop"


def main() -> int:
    fuzz_args = [a for a in sys.argv[1:] if a.startswith("-")] or ["-runs=500"]
    out = "/tmp/sf-t6"
    run_atheris(vulnshop_adapter(f"{out}/vulnshop.db"),
                root=ROOT,
                argv=["acceptance", "-verbosity=1", *fuzz_args],
                trace_path=f"{out}/trace.jsonl",
                corpus_dir=f"{out}/corpus")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
