"""Validate the SS-07F five-app matrix before any freeze claim.

The validator is deliberately conservative.  A complete matrix is necessary
but not sufficient for a blind claim: the app adapters/contracts must have
been pre-registered before the runs, and the SINGLE/SEQUENCE/C/D arms must
share a declared budget.  If those conditions are not evidenced, freeze is
refused rather than writing a misleading manifest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APPS = ("microblog", "ctfd", "healthchecks", "linkding", "spirit")
SEEDS = (1, 2, 3)
BUDGET = 40


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def validate(matrix_path: Path, replay_root: Path) -> dict:
    matrix = _load(matrix_path)
    checks: dict[str, dict] = {}

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks[name] = {"ok": bool(ok), "detail": detail}

    check("budget-40", matrix.get("budget") == BUDGET,
          f"budget={matrix.get('budget')}")
    check("five-apps", set(matrix.get("STATE_GUIDED", {})) == set(APPS),
          str(sorted(matrix.get("STATE_GUIDED", {}))))
    check("three-seeds", tuple(matrix.get("seeds", ())) == SEEDS,
          str(matrix.get("seeds")))
    for arm in ("STATE_GUIDED", "STATE_REUSE"):
        ok = all(matrix.get(arm, {}).get(app, {}).get(str(seed), {}).get(
            "target") for app in APPS for seed in SEEDS)
        check(f"{arm.lower()}-15-of-15", ok)
    replay_ok = all((replay_root / app / "replay.json").exists()
                    and _load(replay_root / app / "replay.json").get(
                        "suffix_target_covered") for app in APPS)
    check("independent-replay-5-of-5", replay_ok)
    # Blind pre-registration cannot be reconstructed from later artifacts.
    check("blind-preregistration", False,
          "adapters/contracts were authored while developing, not pre-registered "
          "before the five-app runs; no blind claim permitted")
    return {
        "ok": all(c["ok"] for c in checks.values()),
        "checks": checks,
        "matrix": str(matrix_path),
        "replay_root": str(replay_root),
        "apps": list(APPS),
        "seeds": list(SEEDS),
        "budget": BUDGET,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix", default=str(
        ROOT / "artifacts/session_ss07f/FAIR_MATRIX.json"))
    parser.add_argument("--replay-root", default=str(
        ROOT / "artifacts/session_ss07f/independent_replay"))
    parser.add_argument("--out", default=str(
        ROOT / "artifacts/session_ss07f/FREEZE_REFUSED.json"))
    args = parser.parse_args(argv)
    result = validate(Path(args.matrix), Path(args.replay_root))
    result["validated_at"] = time.time()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if result["ok"]:
        manifest = dict(result, protocol="ss07-five-apps-v1", frozen=True,
                        blind_fuzzing_claimed=True)
        out.with_name("FREEZE_MANIFEST.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1),
            encoding="utf-8")
        print("FROZEN")
        return 0
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print("REFUSED", json.dumps(result["checks"], ensure_ascii=False))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
