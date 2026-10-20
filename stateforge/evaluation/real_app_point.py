"""Run one (app, seed) C/D point for the SS-07F real-app protocol."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

APPS = {
    "microblog": {
        "adapter": "stateforge/targets/real_apps/microblog/inproc_target.py",
        "source": "external/microblog/app",
        "contract": "artifacts/session_ss07f/microblog/contract.json",
        "pool": "artifacts/session_ss07f/microblog/request_pool.json",
        "target": "main/routes.py::index::29-29::block::1",
        "source_file": "external/microblog/app/main/routes.py",
        "suffix": {"method": "GET", "path": "/index"},
    },
    "ctfd": {
        "adapter": "stateforge/targets/real_apps/ctfd/inproc_target.py",
        "source": "external/CTFd/CTFd",
        "contract": "artifacts/session_ss07f/ctfd/contract.json",
        "pool": "artifacts/session_ss07f/ctfd/request_pool.json",
        "target": "challenges.py::listing::24-24::entry::1",
        "source_file": "external/CTFd/CTFd/challenges.py",
        "suffix": {"method": "GET", "path": "/challenges"},
    },
    "healthchecks": {
        "adapter": "stateforge/targets/real_apps/healthchecks/inproc_target.py",
        "source": "external/healthchecks/hc",
        "contract": "artifacts/session_ss07f/healthchecks/contract.json",
        "pool": "artifacts/session_ss07f/healthchecks/request_pool.json",
        "target": "accounts/views.py::profile::288-288::entry::1",
        "source_file": "external/healthchecks/hc/accounts/views.py",
        "suffix": {"method": "GET", "path": "/accounts/profile/"},
    },
    "linkding": {
        "adapter": "stateforge/targets/real_apps/linkding/inproc_target.py",
        "source": "external/linkding/bookmarks",
        "contract": "artifacts/session_ss07f/linkding/contract.json",
        "pool": "artifacts/session_ss07f/linkding/request_pool.json",
        "target": "views/bookmarks.py::index::43-43::entry::1",
        "source_file": "external/linkding/bookmarks/views/bookmarks.py",
        "suffix": {"method": "GET", "path": "/bookmarks"},
    },
    "spirit": {
        "adapter": "stateforge/targets/real_apps/spirit/inproc_target.py",
        "source": "external/Spirit/spirit",
        "contract": "artifacts/session_ss07f/spirit/contract.json",
        "pool": "artifacts/session_ss07f/spirit/request_pool.json",
        "target": "user/views.py::update::24-24::entry::1",
        "source_file": "external/Spirit/spirit/user/views.py",
        "suffix": {"method": "GET", "path": "/user/"},
    },
}


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(f"point_{path.stem}", path)
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
    from stateforge.solving.session_replay import (
        preparation_checkpoint_from_guided_result, replay_session_checkpoint,
        suffix_without_prefix_reaches_target)

    adapter = _load(ROOT / cfg["adapter"])
    source = ROOT / cfg["source"]
    ir = build_icfg(source)
    pool = load_request_pool(ROOT / cfg["pool"])
    c_ledger = ExecutionCostLedger(
        out_dir=out / "c", run_id=f"{args.app}-{args.seed}-c",
        arm="STATE_GUIDED", seed=args.seed, app=cfg["source"],
        target_block=cfg["target"], request_limit=80)
    c_target = c_ledger.wrap_target(adapter.target, phase="bootstrap")
    c_reset = c_ledger.wrap_reset(adapter.reset_state)
    c_probe = c_ledger.wrap_observer(adapter.session_view)
    c_ledger.begin_sequence("bootstrap")
    result = bootstrap_session_guided(
        adapter, c_target, ir, source_dir=source, target_block=cfg["target"],
        request_pool=pool, corpus_dir=out / "corpus", out_dir=out / "c",
        max_steps=3, max_execs=args.max_execs, seed=args.seed,
        guided=True, session_contract=ROOT / cfg["contract"])
    c_ledger.end_sequence()
    c_costs = c_ledger.finish()
    record = {
        "app": args.app, "seed": args.seed,
        "c": {"found": result.found, "status": result.status,
              "executions": result.executions,
              "steps": list(result.steps), "costs": c_costs},
        "d": None,
    }
    if result.found:
        d_ledger = ExecutionCostLedger(
            out_dir=out / "d", run_id=f"{args.app}-{args.seed}-d",
            arm="STATE_REUSE", seed=args.seed, app=cfg["source"],
            target_block=cfg["target"], request_limit=40)
        d_target = d_ledger.wrap_target(adapter.target, phase="checkpoint")
        d_reset = d_ledger.wrap_reset(adapter.reset_state)
        d_probe = d_ledger.wrap_observer(adapter.session_view)
        ckpt = preparation_checkpoint_from_guided_result(
            result, ROOT / cfg["source_file"], ir, target=d_target,
            reset_hook=d_reset, session_probe=d_probe, trace_root=source)
        if ckpt is not None:
            d_ledger.begin_sequence("checkpoint")
            validation = replay_session_checkpoint(
                ckpt, adapter.target, ir, reset_hook=d_reset,
                session_probe=d_probe, trace_root=source, suffix_steps=[],
                cost_ledger=d_ledger, baseline=ckpt.baseline_fingerprint)
            d_ledger.end_sequence()
            d_ledger.begin_sequence("suffix")
            primary = replay_session_checkpoint(
                ckpt, adapter.target, ir, reset_hook=d_reset,
                session_probe=d_probe, trace_root=source,
                suffix_steps=[cfg["suffix"]], cost_ledger=d_ledger,
                baseline=ckpt.baseline_fingerprint)
            d_ledger.end_sequence()
            d_ledger.begin_sequence("isolation")
            control = d_ledger.wrap_target(adapter.target, phase="suffix")
            isolated = not suffix_without_prefix_reaches_target(
                ckpt, control, ir, reset_hook=d_reset, trace_root=source,
                suffix_steps=[cfg["suffix"]])
            d_ledger.end_sequence()
            d_costs = d_ledger.finish()
            ckpt.save(out / "d")
            record["d"] = {
                "status": primary.status,
                "preparation_verified": primary.preparation_verified,
                "suffix_target_covered": primary.suffix_target_covered,
                "isolation": isolated,
                "prefix_len": ckpt.prefix_len,
                "checkpoint_id": ckpt.checkpoint_id,
                "validation": validation.status,
                "costs": d_costs,
            }
    (out / "point.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(record, ensure_ascii=False))
    return 0 if (record["c"]["found"] and record["d"]
                 and record["d"]["suffix_target_covered"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
