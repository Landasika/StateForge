"""Run the dynamic-completion loop over every target and report the yield.

Each target runs inside its own virtualenv: a Django app imports only under the
interpreter its wheels were built for.  So this script is a *parent* that shells
out to ``<app>/.venv/bin/python`` once per target, rather than importing them
all into one process.

What is measured, and why each number is there:

* ``static``/``learned`` -- unresolved call sites before the trace, and how many
  the trace settled.  The second is the point of dynamic completion.
* ``edges`` -- the graph got bigger, which is the repair actually landing.
* ``events`` -- guards against the failure mode where a trace looks impressive
  but describes installed code the graph never contained.
* ``mapped`` -- events that name a function the IR contains.  A high count here
  is what makes ``learned`` meaningful rather than coincidental.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

REPO = Path("/home/landasika/icassp")
EXTERNAL = REPO / "external"

#: Target label -> application tree.  Labels follow ``targets/apps20/<label>``.
TARGETS = {
    "bakerydemo": "bakerydemo",
    "benchmarkpython": "BenchmarkPython",
    "ctfd": "CTFd",
    "fava": "fava",
    "healthchecks": "healthchecks",
    "linkding": "linkding",
    "mezzanine": "mezzanine",
    "microblog": "microblog",
    "moin": "moin",
    "octoprint": "OctoPrint",
    "oscar": "django-oscar",
    "radicale": "Radicale",
    "realworld": "realworld-django-ninja",  # 无 harness.py（空包）
    "spirit": "Spirit",
    "trac": "trac",
    "wger": "wger",
    "zinnia": "django-blog-zinnia",
}

CHILD = r'''
import json, sys
sys.path.insert(0, {repo!r})
from pathlib import Path
from stateforge.analysis.static.icfg import build_icfg
from stateforge.fuzzing.dynamic_collect import collect_target

label, root = {label!r}, Path({root!r})
ir0 = build_icfg(root)
out = {{"static": len(ir0.metadata["unresolved_calls"]),
        "functions": len(ir0.functions),
        "edges": len(ir0.edges)}}
try:
    c = collect_target(label, ir0, root)
except Exception as exc:
    out["error"] = f"{{type(exc).__name__}}: {{exc}}"
    print("RESULT" + json.dumps(out)); sys.exit(0)

out.update(events=c.event_count, learned=len(c.learned),
           requests=len(c.requests), error=c.error or "")
if c.learned:
    ir1 = build_icfg(root, dynamic=c.learned)
    out["after"] = len(ir1.metadata["unresolved_calls"])
    out["edges_after"] = len(ir1.edges)
else:
    out["after"] = out["static"]; out["edges_after"] = out["edges"]

fns = {{(f.file, f.name.rsplit(".", 1)[-1]) for f in ir0.functions.values()}}
out["mapped"] = sum(1 for e in c.events if (e.file, e.function) in fns)
print("RESULT" + json.dumps(out))
'''


def interpreter(root: Path) -> str | None:
    for cand in (root / ".venv" / "bin" / "python",
                 root / "venv" / "bin" / "python"):
        if cand.is_file():
            return str(cand)
    return None


def run_one(label: str, dirname: str) -> dict:
    root = EXTERNAL / dirname
    if not root.is_dir():
        return {"label": label, "error": "missing"}
    py = interpreter(root)
    if py is None:
        return {"label": label, "error": "no venv"}
    code = CHILD.format(repo=str(REPO), label=label, root=str(root))
    try:
        proc = subprocess.run([py, "-c", code], capture_output=True,
                              text=True, timeout=1800, cwd=str(REPO))
    except subprocess.TimeoutExpired:
        return {"label": label, "error": "timeout"}
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT"):
            row = json.loads(line[len("RESULT"):])
            row["label"] = label
            return row
    tail = (proc.stderr or "").strip().splitlines()
    return {"label": label, "error": tail[-1] if tail else "no output"}


def main() -> int:
    rows = []
    for label, dirname in TARGETS.items():
        row = run_one(label, dirname)
        rows.append(row)
        print(f"  {label:16s} {row}", file=sys.stderr, flush=True)

    print(f"{'target':16s} {'static':>7s} {'->after':>8s} {'closed':>7s} "
          f"{'ratio':>6s} {'events':>8s} {'mapped':>7s} {'edges':>13s}")
    tot_s = tot_a = tot_ev = tot_m = 0
    for r in rows:
        r.setdefault("label", "?")
        if "static" not in r or "after" not in r:
            print(f"{r['label']:16s} 跳过: {r.get('error')}")
            continue
        s, a = r["static"], r["after"]
        closed = s - a
        ratio = f"{closed / s:.1%}" if s else "--"
        tot_s += s; tot_a += a; tot_ev += r["events"]; tot_m += r["mapped"]
        print(f"{r['label']:16s} {s:7d} {a:8d} {closed:7d} {ratio:>6s} "
              f"{r['events']:8d} {r['mapped']:7d} "
              f"{r['edges']:6d}->{r['edges_after']:<6d}")
    closed = tot_s - tot_a
    pct = f"{closed / tot_s:.1%}" if tot_s else "--"
    print(f"\n合计 未解析 {tot_s} -> {tot_a}  关闭 {closed} ({pct})  "
          f"事件 {tot_ev}  可映射 {tot_m}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
