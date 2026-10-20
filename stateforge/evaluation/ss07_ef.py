"""SS-07E/F: frozen five-app pre-registration and launch matrix.

This is the honest boundary of the current environment: launch health can be
verified for four apps, but no real-app Session target adapter is available
for the frozen four-arm fuzzing protocol.  The report records zero arm runs and
does not freeze an experiment that never ran.
"""

from __future__ import annotations

import json
from pathlib import Path

from stateforge.evaluation.real_apps import pre_register, run_all

ROOT = Path(__file__).resolve().parents[2]
APP_NAMES = ("CTFd", "healthchecks", "linkding", "Spirit", "microblog")


def _per_app_reports(out: Path, result: dict) -> None:
    for r in result["results"]:
        lines = [
            f"# {r['app']} — SS-07E/F raw report", "",
            f"- status: `{r['status']}`",
            f"- reason: `{r['reason']}`",
            f"- source SHA-256: `{r['source_hash']}`",
            f"- pre-registered SHA-256: `{r['pre_registered_hash']}`",
            f"- launch command: `{' '.join(r['launch_command'])}`",
            f"- health URL: `{r['health']}`",
            f"- elapsed wall seconds: `{r['elapsed_wall_seconds']}`",
            f"- raw log: `{r['log']}`", "",
            "## Arm execution", "",
            "This run performed **zero** fuzz arms for this app.  A health",
            "probe is not a blind Session fuzzing result.  The status is",
            "kept separate from `target_reached`; no success is claimed.", "",
        ]
        (out / f"REPORT_{r['app']}.md").write_text("\n".join(lines),
                                                   encoding="utf-8")


def run(out: str | Path) -> dict:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    pre = pre_register(out)                       # before any launch
    result = run_all(out)
    _per_app_reports(out, result)
    blockers = [r for r in result["results"] if r["status"] != "SMOKE_PASS"]
    arm_runs = sum(r.get("arms_run", 0) for r in result["results"])
    summary = {
        "protocol": "ss07-five-apps-v1",
        "pre_registered_at": pre.get("created_at"),
        "source_revision": pre.get("source_revision"),
        "apps": {r["app"]: r["status"] for r in result["results"]},
        "smoke_pass": result["summary"].get("SMOKE_PASS", 0),
        "env_blocked": result["summary"].get("ENV_BLOCKED", 0),
        "blockers": blockers,
        "arm_runs": arm_runs,
        "blind_fuzzing_claimed": False,
        "frozen": False,
        "freeze_reason": ("no real-app Session target adapter; zero arms ran"
                          if arm_runs == 0 else ""),
    }
    (out / "SS07EF_SUMMARY.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "FREEZE_REFUSED.json").write_text(
        json.dumps({"frozen": False, "reason": summary["freeze_reason"],
                    "arm_runs": arm_runs}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    return {"pre": pre, "result": result, "summary": summary}
