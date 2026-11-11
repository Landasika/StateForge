"""Measure the closure rate inside what the trace actually reached.

The headline number -- unresolved sites closed / all unresolved sites -- mixes
two different things.  A site inside a function the trace never entered cannot
be settled by any run, so counting it against the dynamic pass understates it,
and counting it in favour would overstate it.  The honest denominator is the
sites whose *caller* was executed: those are the ones a run had a chance to
answer.

Reported per target:

* ``open``   -- unresolved sites before the trace
* ``in reach`` -- of those, how many sit in a function the trace entered
* ``closed`` -- how many the trace settled
* ``closure`` -- closed / in reach, the rate that is actually attributable
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path("/home/landasika/icassp")
EXTERNAL = REPO / "external"

TARGETS = {
    "mezzanine": "mezzanine",
    "microblog": "microblog",
    "healthchecks": "healthchecks",
    "zinnia": "django-blog-zinnia",
    "wger": "wger",
    "ctfd": "CTFd",
}

CHILD = r'''
import json, sys
sys.path.insert(0, {repo!r})
from pathlib import Path
from stateforge.analysis.static.icfg import build_icfg
from stateforge.fuzzing.dynamic_collect import collect_target

root = Path({root!r})
ir0 = build_icfg(root)
c = collect_target({label!r}, ir0, root)
if not c.learned:
    print("RESULT" + json.dumps({{"label": {label!r}, "error": "nothing learned"}}))
    raise SystemExit(0)

# Which functions did the trace actually enter?
entered = {{(e.file, e.function) for e in c.events}}
by_name = {{}}
for f in ir0.functions.values():
    by_name.setdefault(f.file, set()).add(f.name.rsplit(".", 1)[-1])
reached_files = entered

# An unresolved site counts as "in reach" when its caller was executed.
callers = {{}}
for row in ir0.metadata["unresolved_calls"]:
    callers.setdefault(row.get("caller"), []).append(row)

def caller_key(qname):
    """The trace names a function by file plus short name; match on that."""
    hits = []
    for (f_file, short) in reached_files:
        full = qname.rsplit(".", 1)[-1]
        if short == full:
            hits.append((f_file, short))
    return hits

open_sites = ir0.metadata["unresolved_calls"]
in_reach = 0
for row in open_sites:
    if caller_key(row.get("caller") or ""):
        in_reach += 1

ir1 = build_icfg(root, dynamic=c.learned)
print("RESULT" + json.dumps({{
    "label": {label!r},
    "open": len(open_sites),
    "in_reach": in_reach,
    "closed": len(c.learned),
    "events": c.event_count,
    "functions": len(ir0.functions),
    "entered": len(reached_files),
}}))
'''


def interpreter(root: Path) -> str | None:
    cand = root / ".venv" / "bin" / "python"
    return str(cand) if cand.is_file() else None


for label, dirname in TARGETS.items():
    root = EXTERNAL / dirname
    py = interpreter(root)
    if py is None:
        print(f"{label:14s} 无 venv", file=sys.stderr)
        continue
    code = CHILD.format(repo=str(REPO), label=label, root=str(root))
    proc = subprocess.run([py, "-c", code], capture_output=True, text=True,
                          timeout=1800, cwd=str(REPO))
    row = None
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT"):
            row = json.loads(line[len("RESULT"):])
    if row is None:
        tail = (proc.stderr or "").strip().splitlines()
        row = {"label": label, "error": tail[-1] if tail else "no output"}
    print(f"  {label:14s} {row}", file=sys.stderr, flush=True)

    if "error" in row:
        print(f"{label:14s} {row['error']}")
        continue
    rate = row["closed"] / row["in_reach"] if row["in_reach"] else 0.0
    cover = row["entered"] / row["functions"] if row["functions"] else 0.0
    print(f"{label:14s} open {row['open']:5d}  in-reach {row['in_reach']:5d}  "
          f"closed {row['closed']:4d}  closure {rate:6.1%}  "
          f"functions entered {row['entered']:4d}/{row['functions']:<4d} ({cover:.0%})")
