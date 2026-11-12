"""Probe all ten candidate apps for a working in-process harness.

Every harness under ``targets/apps20/<slug>/`` exposes ``smoke()`` returning
``[(path, status), ...]``.  Importing one requires that app's own virtualenv,
because each harness imports the real application and its dependencies.  So
each probe runs in a child process driven by ``external/<app>/.venv/bin/python``
rather than in this interpreter.

The question this answers is narrow and useful: *which apps can be driven at
all today?*  A probe that hangs, crashes on import, or returns no routes with a
2xx/3xx status is a real datapoint -- it bounds the denominator of any
ten-app experiment before a single number is measured.

Usage::

    python probe_apps_launch.py            # all ten
    python probe_apps_launch.py fava wger  # selected
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

# slug -> (source dir under external/, venv relative to that dir)
APPS: dict[str, str] = {
    "ctfd": "CTFd",
    "linkding": "linkding",
    "fava": "fava",
    "wger": "wger",
    "microblog": "microblog",
    "octoprint": "OctoPrint",
    "spirit": "Spirit",
    "zinnia": "django-blog-zinnia",
    "mezzanine": "mezzanine",
    "oscar": "django-oscar",
}

# Child program: import the harness, call smoke(), print a JSON line.
PROBE = r'''
import json, sys, traceback
sys.path.insert(0, {repo!r})
try:
    mod = __import__("stateforge.targets.apps20.{slug}.harness",
                     fromlist=["smoke", "ROUTES"])
    routes = getattr(mod, "ROUTES", None)
    n_routes = len(routes) if routes is not None else None
    smoke = getattr(mod, "smoke", None)
    if smoke is None:
        print(json.dumps({{"ok": False, "stage": "no_smoke",
                          "detail": "harness has no smoke()",
                          "routes": n_routes}}))
        sys.exit(0)
    results = smoke()
    print(json.dumps({{"ok": True, "stage": "smoke", "routes": n_routes,
                      "probed": len(results),
                      "results": [list(map(str, r)) for r in results]}}))
except BaseException:
    print(json.dumps({{"ok": False, "stage": "exception",
                      "detail": traceback.format_exc()[-1500:]}}))
'''


def probe(slug: str, timeout: int = 180) -> dict:
    src = REPO / "external" / APPS[slug]
    python = src / ".venv" / "bin" / "python"
    if not python.exists():
        return {"ok": False, "stage": "no_venv",
                "detail": f"missing {python.relative_to(REPO)}"}
    code = PROBE.format(repo=str(REPO), slug=slug)
    started = time.monotonic()
    try:
        proc = subprocess.run([str(python), "-c", code], capture_output=True,
                              text=True, timeout=timeout, cwd=str(REPO))
    except subprocess.TimeoutExpired:
        return {"ok": False, "stage": "timeout",
                "detail": f"no result within {timeout}s",
                "elapsed": round(time.monotonic() - started, 1)}
    elapsed = round(time.monotonic() - started, 1)
    line = ""
    for candidate in reversed(proc.stdout.strip().splitlines()):
        if candidate.startswith("{"):
            line = candidate
            break
    if not line:
        return {"ok": False, "stage": "no_output", "elapsed": elapsed,
                "stderr": proc.stderr.strip()[-1200:]}
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return {"ok": False, "stage": "bad_output", "elapsed": elapsed,
                "stdout": proc.stdout[-800:]}
    payload["elapsed"] = elapsed
    if proc.returncode != 0:
        payload.setdefault("detail", "")
        payload["detail"] += f"\n(exit {proc.returncode})"
    return payload


def classify(payload: dict) -> str:
    """Reduce a probe result to one word, so the table is readable.

    A smoke that returns only 4xx is not a working harness: the app started but
    every route rejected the request, which means the harness's own fixtures
    are not establishing the state it expects.
    """
    if not payload.get("ok"):
        stage = payload.get("stage")
        return {"no_venv": "NO_VENV", "timeout": "TIMEOUT",
                "no_smoke": "NO_SMOKE", "no_output": "IMPORT_FAIL",
                "bad_output": "IMPORT_FAIL",
                "exception": "IMPORT_FAIL"}.get(stage, "ERROR")
    results = payload.get("results") or []
    if not results:
        return "NO_ROUTES"
    codes = [str(r[1]) for r in results]
    good = [c for c in codes if c[:1] in ("2", "3")]
    if len(good) == len(codes):
        return "OK"
    if good:
        return "PARTIAL"
    return "ALL_REJECTED"


def main() -> int:
    wanted = sys.argv[1:] or list(APPS)
    unknown = [s for s in wanted if s not in APPS]
    if unknown:
        print(f"unknown app(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    rows = []
    for slug in wanted:
        print(f"probing {slug} ...", flush=True)
        payload = probe(slug)
        verdict = classify(payload)
        rows.append((slug, verdict, payload))
        detail = ""
        if verdict in ("IMPORT_FAIL", "NO_VENV", "TIMEOUT", "ERROR"):
            tail = (payload.get("detail") or payload.get("stderr") or "")
            tail = " ".join(tail.split())[-200:]
            detail = f"  {tail}"
        print(f"  -> {verdict}{detail}", flush=True)

    print("\n" + "=" * 72)
    print(f"{'app':<12} {'verdict':<14} {'routes':>6} {'probed':>6} "
          f"{'elapsed':>8}  status codes")
    print("-" * 72)
    for slug, verdict, payload in rows:
        routes = payload.get("routes")
        probed = payload.get("probed")
        elapsed = payload.get("elapsed")
        codes = ""
        if payload.get("results"):
            codes = " ".join(str(r[1]) for r in payload["results"][:8])
            if len(payload["results"]) > 8:
                codes += " ..."
        print(f"{slug:<12} {verdict:<14} {str(routes or '-'):>6} "
              f"{str(probed or '-'):>6} {str(elapsed or '-'):>8}  {codes}")

    usable = [s for s, v, _ in rows if v in ("OK", "PARTIAL")]
    print("-" * 72)
    print(f"usable harnesses: {len(usable)}/{len(rows)}  "
          f"({', '.join(usable) if usable else 'none'})")
    blocked = [(s, v) for s, v, _ in rows if v not in ("OK", "PARTIAL")]
    if blocked:
        print("blocked: " + ", ".join(f"{s}({v})" for s, v in blocked))

    out = REPO / "docs/reports/dynamic-completion-2026-11-11/apps_launch_probe.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        [{"app": s, "verdict": v, **p} for s, v, p in rows],
        indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {out.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
