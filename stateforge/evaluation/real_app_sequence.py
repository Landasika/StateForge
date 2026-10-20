"""Run the structural SEQUENCE arm for one real app and seed."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
from stateforge.evaluation.real_app_point import APPS  # noqa: E402


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(f"seq_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("app", choices=sorted(APPS))
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--max-execs", type=int, default=40)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    cfg = APPS[args.app]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    from stateforge.analysis.static.icfg import build_icfg
    from stateforge.fuzzing.session_guided import (bootstrap_session_guided,
                                                   load_request_pool)
    from stateforge.runtime.execution_costs import ExecutionCostLedger

    adapter = _load(ROOT / cfg["adapter"])
    source = ROOT / cfg["source"]
    ir = build_icfg(source)
    pool = load_request_pool(ROOT / cfg["pool"])
    ledger = ExecutionCostLedger(
        out_dir=out, run_id=f"{args.app}-{args.seed}-seq",
        arm="SEQUENCE", seed=args.seed, app=cfg["source"],
        target_block=cfg["target"], request_limit=80)
    target = ledger.wrap_target(adapter.target, phase="bootstrap")
    reset = ledger.wrap_reset(adapter.reset_state)
    probe = ledger.wrap_observer(adapter.session_view)
    ledger.begin_sequence("bootstrap")
    result = bootstrap_session_guided(
        adapter, target, ir, source_dir=source, target_block=cfg["target"],
        request_pool=pool, corpus_dir=out / "corpus", out_dir=out,
        max_steps=3, max_execs=args.max_execs, seed=args.seed,
        guided=False, session_contract=ROOT / cfg["contract"])
    ledger.end_sequence()
    costs = ledger.finish()
    record = {
        "app": args.app, "seed": args.seed, "arm": "SEQUENCE",
        "found": result.found, "status": result.status,
        "executions": result.executions, "steps": list(result.steps),
        "costs": costs,
    }
    (out / "result.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(record, ensure_ascii=False))
    return 0 if result.found else 1


if __name__ == "__main__":
    raise SystemExit(main())
