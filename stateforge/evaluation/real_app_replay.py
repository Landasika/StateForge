"""Independent-process replay for the five SS-07F real-app checkpoints.

Run this module in a fresh interpreter, in the target application's own
virtualenv.  It loads a saved checkpoint, re-opens the real app, and replays
the verified prefix + one suffix.  It never consults the experiment process.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

APPS = {
    "microblog": {
        "adapter": "stateforge/targets/real_apps/microblog/inproc_target.py",
        "source": "external/microblog/app",
        "target": "main/routes.py::index::29-29::block::1",
        "suffix": {"method": "GET", "path": "/index"},
    },
    "ctfd": {
        "adapter": "stateforge/targets/real_apps/ctfd/inproc_target.py",
        "source": "external/CTFd/CTFd",
        "target": "challenges.py::listing::24-24::entry::1",
        "suffix": {"method": "GET", "path": "/challenges"},
    },
    "healthchecks": {
        "adapter": "stateforge/targets/real_apps/healthchecks/inproc_target.py",
        "source": "external/healthchecks/hc",
        "target": "accounts/views.py::profile::288-288::entry::1",
        "suffix": {"method": "GET", "path": "/accounts/profile/"},
    },
    "linkding": {
        "adapter": "stateforge/targets/real_apps/linkding/inproc_target.py",
        "source": "external/linkding/bookmarks",
        "target": "views/bookmarks.py::index::43-43::entry::1",
        "suffix": {"method": "GET", "path": "/bookmarks"},
    },
    "spirit": {
        "adapter": "stateforge/targets/real_apps/spirit/inproc_target.py",
        "source": "external/Spirit/spirit",
        "target": "user/views.py::update::24-24::entry::1",
        "suffix": {"method": "GET", "path": "/user/"},
    },
}


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(f"replay_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("app", choices=sorted(APPS))
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    cfg = APPS[args.app]
    sys.path.insert(0, str(ROOT))
    from stateforge.analysis.static.icfg import build_icfg
    from stateforge.runtime.execution_costs import ExecutionCostLedger
    from stateforge.solving.session_replay import (
        SessionReplayCheckpoint, replay_session_checkpoint)

    adapter = _load(ROOT / cfg["adapter"])
    ir = build_icfg(ROOT / cfg["source"])
    ckpt = SessionReplayCheckpoint.load(args.checkpoint)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ledger = ExecutionCostLedger(
        out_dir=out, run_id=f"replay-{args.app}", arm="REPLAY",
        seed=0, app=cfg["source"], target_block=cfg["target"],
        request_limit=20)
    reset = ledger.wrap_reset(adapter.reset_state)
    probe = ledger.wrap_observer(adapter.session_view)
    ledger.begin_sequence("replay")
    result = replay_session_checkpoint(
        ckpt, adapter.target, ir, reset_hook=reset, session_probe=probe,
        trace_root=ROOT / cfg["source"], suffix_steps=[cfg["suffix"]],
        cost_ledger=ledger, baseline=ckpt.baseline_fingerprint)
    ledger.end_sequence()
    costs = ledger.finish()
    data = {
        "app": args.app,
        "checkpoint": args.checkpoint,
        "status": result.status,
        "preparation_verified": result.preparation_verified,
        "suffix_target_covered": result.suffix_target_covered,
        "conditions_verified": result.conditions_verified,
        "prefix_len": result.prefix_len,
        "suffix_len": result.suffix_len,
        "costs": costs,
    }
    (out / "replay.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(data, ensure_ascii=False))
    return 0 if result.suffix_target_covered else 1


if __name__ == "__main__":
    raise SystemExit(main())
