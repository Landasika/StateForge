"""SS-07D: controlled regression, online causality, and freeze gate.

The freeze gate is deliberately boring: it refuses to write a frozen manifest
when any arm violates the physical-request budget, D did bootstrap, a VERIFIED
success lacks a checkpoint, wall clocks are missing, or online decisions were
not consumed by later inputs.  A failed run is preserved, never "repaired".
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

REQUIRED_ARMS = ("SINGLE", "SEQUENCE", "STATE_GUIDED", "STATE_REUSE")


class FreezeRefused(RuntimeError):
    pass


def _sha(path: str | Path) -> str:
    p = Path(path)
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return "missing"


def _root_request_count(out_root: Path) -> int:
    path = out_root / "per_request.jsonl"
    if not path.exists():
        return -1
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            if json.loads(line).get("event") == "request":
                n += 1
        except json.JSONDecodeError:
            return -1
    return n


def validate_controlled(out_root: str | Path, runs: list[dict], *,
                        budget: int, seeds: tuple[int, ...],
                        require_online: bool = True) -> dict:
    out_root = Path(out_root)
    checks: dict[str, dict] = {}

    def put(name, ok, detail=""):
        checks[name] = {"ok": bool(ok), "detail": detail}

    by_arm = {a: [r for r in runs if r.get("arm") == a]
              for a in REQUIRED_ARMS}
    put("all_arms_equal_seed_count",
        all(len(by_arm[a]) == len(seeds) for a in REQUIRED_ARMS),
        json.dumps({a: len(by_arm[a]) for a in REQUIRED_ARMS}))
    put("all_seeds_present",
        all({r.get("seed") for r in by_arm[a]} == set(seeds)
            for a in REQUIRED_ARMS))

    over = [r for r in runs
            if int(r.get("target_http_requests", 0)) > budget]
    put("physical_request_budget", not over,
        f"over={[(r.get('arm'), r.get('seed'), r.get('target_http_requests')) for r in over]}")

    zero_wall = [r for r in runs
                 if not (r.get("elapsed_wall_seconds") or 0) > 0]
    put("wall_clock_measured", not zero_wall,
        f"zero={[(r.get('arm'), r.get('seed')) for r in zero_wall]}")

    d_boot = [r for r in by_arm["STATE_REUSE"]
              if int(r.get("bootstrap_http_requests", 0)) != 0]
    put("d_no_bootstrap", not d_boot,
        f"boot={[(r.get('seed'), r.get('bootstrap_http_requests')) for r in d_boot]}")

    false_success = []
    for r in by_arm["STATE_REUSE"]:
        if r.get("target_reached") and not r.get("session_condition_verified"):
            false_success.append((r.get("seed"), "target-without-verified-prep"))
        if r.get("target_reached") and not r.get("checkpoint_id"):
            false_success.append((r.get("seed"), "target-without-checkpoint-id"))
        if r.get("failures") and "NO_CHECKPOINT" in r["failures"]:
            # A legitimate no-checkpoint failure is allowed, but it must not
            # be reported as a successful reuse.
            if r.get("target_reached"):
                false_success.append((r.get("seed"), "no-checkpoint-but-success"))
    put("d_checkpoint_bound", not false_success, json.dumps(false_success))

    root_n = _root_request_count(out_root)
    per_run_n = sum(int(r.get("target_http_requests", 0)) for r in runs)
    put("root_logs_recomputable", root_n >= 0 and root_n == per_run_n,
        f"root={root_n} per_run={per_run_n}")

    if require_online:
        c_runs = by_arm["STATE_GUIDED"]
        consumed = sum(int((r.get("online_summary") or {}).get(
            "consumed_mutations", 0)) for r in c_runs)
        switches = sum(int((r.get("online_summary") or {}).get(
            "switches", 0)) for r in c_runs)
        put("online_consumed_after_start", consumed >= 2,
            f"consumed={consumed} switches={switches}")
        put("online_feedback_changed_focus", switches >= 1,
            f"switches={switches}")

    ok = all(v["ok"] for v in checks.values())
    return {"ok": ok, "checks": checks,
            "run_count": len(runs), "budget": budget,
            "seeds": list(seeds), "checked_at": time.time()}


def freeze(out_root: str | Path, runs: list[dict], *, budget: int,
           seeds: tuple[int, ...], source_paths: list[str | Path],
           test_command: str, test_result: str,
           protocol: str = "ss07-controlled-v1",
           require_online: bool = True) -> dict:
    out_root = Path(out_root)
    validation = validate_controlled(
        out_root, runs, budget=budget, seeds=seeds,
        require_online=require_online)
    if not validation["ok"]:
        (out_root / "freeze_refused.json").write_text(
            json.dumps(validation, ensure_ascii=False, indent=1),
            encoding="utf-8")
        raise FreezeRefused("controlled checks failed: " + ", ".join(
            k for k, v in validation["checks"].items() if not v["ok"]))
    manifest = {
        "protocol": protocol,
        "frozen": True,
        "frozen_at": time.time(),
        "source_revision": _git_revision(),
        "source_hashes": {str(p): _sha(p) for p in source_paths},
        "protocol_file_hash": _sha(__file__),
        "test_command": test_command,
        "test_result": test_result,
        "validation": validation,
        "run_count": len(runs),
        "budget": budget,
        "seeds": list(seeds),
        "raw_logs": {
            "per_request": str(out_root / "per_request.jsonl"),
            "per_sequence": str(out_root / "per_sequence.jsonl"),
            "scheduler": str(out_root / "scheduler.jsonl"),
            "per_run": str(out_root / "per_run.jsonl"),
        },
    }
    (out_root / "freeze_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return manifest


def _git_revision() -> str:
    import subprocess
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True,
            stderr=subprocess.DEVNULL).strip()
    except Exception:                              # noqa: BLE001
        return "unknown"
