"""Supplementary basic-block coverage measurement for the SS-07F arms.

This experiment is OUTSIDE the frozen SS-07F protocol: it re-runs each arm's
deterministic logic (seeded search / checkpoint replay / pool sampling) under
coverage.py, maps executed lines onto ICFG basic blocks, and reports covered
blocks over the ICFG denominator per app source tree.

It never modifies files whose hashes are frozen by the held-out
pre-registration; all outputs go to a separate coverage_supp directory.

Coverage definition: a basic block (parsed from the ICFG block id
``file::function::start-end::kind::index``) counts as covered when ANY line
inside its span was executed.  The SINGLE arm is approximated by 40 random
single requests drawn from the same request pool with the same seed, because
Atheris's own trace hook conflicts with coverage.py.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from stateforge.evaluation.real_app_point import APPS  # noqa: E402

SPAN_RE = re.compile(r"^(\d+)-(\d+)$")


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(f"cov_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _parse_block(block_id: str):
    """file, (start, end) from an ICFG block id."""
    parts = block_id.split("::")
    if len(parts) < 5:
        return None
    m = SPAN_RE.match(parts[-3])
    if not m:
        return None
    return parts[0], (int(m.group(1)), int(m.group(2)))


def _block_table(ir):
    table = {}
    for block_id in ir.blocks:
        parsed = _parse_block(block_id)
        if parsed:
            table.setdefault(parsed[0], []).append(
                (block_id, parsed[1][0], parsed[1][1]))
    return table


def _run_arm(arm: str, cfg: dict, out: Path, seed: int, adapter, ir):
    from stateforge.analysis.static.icfg import build_icfg  # noqa: F401
    from stateforge.fuzzing.session_guided import (
        SessionGuidedResult, bootstrap_session_guided, load_request_pool)
    from stateforge.runtime.execution_costs import ExecutionCostLedger
    from stateforge.solving.session_replay import (
        SessionReplayCheckpoint, preparation_checkpoint_from_guided_result,
        replay_session_checkpoint, suffix_without_prefix_reaches_target)

    source = ROOT / cfg["source"]
    pool = load_request_pool(ROOT / cfg["pool"])
    requests = 0

    if arm == "single":
        ledger = ExecutionCostLedger(
            out_dir=out, run_id=f"{cfg['app']}-{seed}-single",
            arm="SINGLE", seed=seed, app=cfg["source"],
            target_block=cfg["target"], request_limit=60)
        target = ledger.wrap_target(adapter.target, phase="atheris")
        reset = ledger.wrap_reset(adapter.reset_state)
        rng = random.Random(seed)
        for i in range(40):
            reset()
            ledger.begin_sequence(f"single-{i + 1}")
            step = pool[rng.randrange(len(pool))]
            target(json.dumps({"steps": [step]}).encode())
            ledger.end_sequence()
        costs = ledger.finish()
        return costs

    if arm == "sequence":
        ledger = ExecutionCostLedger(
            out_dir=out, run_id=f"{cfg['app']}-{seed}-seq",
            arm="SEQUENCE", seed=seed, app=cfg["source"],
            target_block=cfg["target"], request_limit=80)
        target = ledger.wrap_target(adapter.target, phase="bootstrap")
        reset = ledger.wrap_reset(adapter.reset_state)
        probe = ledger.wrap_observer(adapter.session_view)
        ledger.begin_sequence("bootstrap")
        result = bootstrap_session_guided(
            adapter, target, ir, source_dir=source,
            target_block=cfg["target"], request_pool=pool,
            corpus_dir=out / "corpus", out_dir=out, max_steps=3,
            max_execs=40, seed=seed, guided=False,
            session_contract=ROOT / cfg["contract"])
        ledger.end_sequence()
        return ledger.finish()

    if arm == "c":
        ledger = ExecutionCostLedger(
            out_dir=out, run_id=f"{cfg['app']}-{seed}-c",
            arm="STATE_GUIDED", seed=seed, app=cfg["source"],
            target_block=cfg["target"], request_limit=80)
        target = ledger.wrap_target(adapter.target, phase="bootstrap")
        reset = ledger.wrap_reset(adapter.reset_state)
        probe = ledger.wrap_observer(adapter.session_view)
        ledger.begin_sequence("bootstrap")
        result = bootstrap_session_guided(
            adapter, target, ir, source_dir=source,
            target_block=cfg["target"], request_pool=pool,
            corpus_dir=out / "corpus", out_dir=out, max_steps=3,
            max_execs=40, seed=seed, guided=True,
            session_contract=ROOT / cfg["contract"])
        ledger.end_sequence()
        return ledger.finish()

    if arm == "d":
        heldout = ROOT / "artifacts/session_ss07f/heldout_v2" / cfg["app"]
        raw = json.loads(
            (heldout / "c/session_guided/result.json").read_text("utf-8"))
        result = SessionGuidedResult(
            target_block=cfg["target"], guided=True, found=raw["found"],
            status=raw["status"], seed=None,
            steps=tuple(raw.get("steps") or ()),
            writer_blocks=tuple(raw.get("writer_blocks") or ()),
            session_witness=raw.get("session_witness"),
            executions=raw.get("executions", 0))
        ledger = ExecutionCostLedger(
            out_dir=out, run_id=f"{cfg['app']}-{seed}-d",
            arm="STATE_REUSE", seed=seed, app=cfg["source"],
            target_block=cfg["target"], request_limit=40)
        target = ledger.wrap_target(adapter.target, phase="checkpoint")
        reset = ledger.wrap_reset(adapter.reset_state)
        probe = ledger.wrap_observer(adapter.session_view)
        ckpt = preparation_checkpoint_from_guided_result(
            result, ROOT / cfg["source_file"], ir, target=target,
            reset_hook=reset, session_probe=probe, trace_root=source)
        if ckpt is None:
            return ledger.finish()
        ledger.begin_sequence("checkpoint")
        replay_session_checkpoint(
            ckpt, adapter.target, ir, reset_hook=reset, session_probe=probe,
            trace_root=source, suffix_steps=[], cost_ledger=ledger,
            baseline=ckpt.baseline_fingerprint)
        ledger.end_sequence()
        ledger.begin_sequence("suffix")
        replay_session_checkpoint(
            ckpt, adapter.target, ir, reset_hook=reset, session_probe=probe,
            trace_root=source, suffix_steps=[cfg["suffix"]],
            cost_ledger=ledger, baseline=ckpt.baseline_fingerprint)
        ledger.end_sequence()
        ledger.begin_sequence("isolation")
        control = ledger.wrap_target(adapter.target, phase="suffix")
        suffix_without_prefix_reaches_target(
            ckpt, control, ir, reset_hook=reset, trace_root=source,
            suffix_steps=[cfg["suffix"]])
        ledger.end_sequence()
        return ledger.finish()

    if arm == "replay":
        heldout = ROOT / "artifacts/session_ss07f/heldout_v2" / cfg["app"]
        ckpt_file = sorted((heldout / "d").glob("sc-*.json"))[0]
        ckpt = SessionReplayCheckpoint.load(ckpt_file)
        ledger = ExecutionCostLedger(
            out_dir=out, run_id=f"replay-{cfg['app']}", arm="REPLAY",
            seed=0, app=cfg["source"], target_block=cfg["target"],
            request_limit=20)
        reset = ledger.wrap_reset(adapter.reset_state)
        probe = ledger.wrap_observer(adapter.session_view)
        ledger.begin_sequence("replay")
        replay_session_checkpoint(
            ckpt, adapter.target, ir, reset_hook=reset, session_probe=probe,
            trace_root=source, suffix_steps=[cfg["suffix"]],
            cost_ledger=ledger, baseline=ckpt.baseline_fingerprint)
        ledger.end_sequence()
        return ledger.finish()

    raise ValueError(f"unknown arm {arm}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("app", choices=sorted(APPS))
    parser.add_argument("arm",
                        choices=["single", "sequence", "c", "d", "replay"])
    parser.add_argument("--seed", type=int, default=5)
    parser.add_argument("--out-root", default=str(
        ROOT / "artifacts/session_ss07f/coverage_supp"))
    args = parser.parse_args(argv)

    # The engine's block-id tracer installs sys.settrace, which silently
    # evicts coverage.py's default settrace core (verified: request handling
    # disappeared from coverage output while the engine still reported the
    # target block as executed).  The sysmon core (PEP 669) coexists with
    # settrace, so the engine tracer and coverage both keep working.
    os.environ.setdefault("COVERAGE_CORE", "sysmon")
    import coverage

    cfg = dict(APPS[args.app])
    cfg["app"] = args.app
    out = Path(args.out_root) / args.app / args.arm
    out.mkdir(parents=True, exist_ok=True)
    source = ROOT / cfg["source"]

    cov = coverage.Coverage(data_file=None, source=[str(source)])
    cov.start()
    try:
        adapter = _load(ROOT / cfg["adapter"])
        from stateforge.analysis.static.icfg import build_icfg
        ir = build_icfg(source)
        costs = _run_arm(args.arm, cfg, out, args.seed, adapter, ir)
    finally:
        cov.stop()

    data = cov.get_data()
    executed = {}
    for fname in data.measured_files():
        rel = os.path.relpath(fname, source)
        if rel.startswith(".."):
            continue
        executed[rel] = set(data.lines(fname) or ())

    table = _block_table(ir)
    covered_blocks, per_file = [], {}
    for rel, blocks in table.items():
        lines = executed.get(rel, set())
        hits = [b for (b, start, end) in blocks
                if any(start <= ln <= end for ln in lines)]
        covered_blocks.extend(hits)
        per_file[rel] = {"total": len(blocks), "covered": len(hits)}

    total = sum(v["total"] for v in per_file.values())
    record = {
        "app": args.app, "arm": args.arm, "seed": args.seed,
        "source": cfg["source"],
        "definition": "ICFG basic block covered if any line in span executed",
        "single_note": ("SINGLE approximated by 40 random single requests "
                        "from the same pool (Atheris trace hook conflicts "
                        "with coverage.py)") if args.arm == "single" else None,
        "total_blocks": total,
        "covered_blocks": len(covered_blocks),
        "percent": round(100.0 * len(covered_blocks) / total, 2) if total
        else 0.0,
        "executed_files": len(executed),
        "per_file": per_file,
        "covered_block_ids": covered_blocks,
        "costs": costs,
    }
    (out / "coverage.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: record[k] for k in
                      ("app", "arm", "total_blocks", "covered_blocks",
                       "percent", "costs")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
