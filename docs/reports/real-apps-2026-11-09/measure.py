"""Consistent per-app dynamic measurement used across the survey."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from stateforge.analysis.dynamic.callgraph import learn_call_edges, unresolved_sites
from stateforge.analysis.static.icfg import build_icfg
from stateforge.fuzzing.feedback.frontier import find_frontiers
from stateforge.fuzzing.feedback.overlay import build_overlay
from stateforge.runtime.mapper import map_trace
from stateforge.runtime.tracer import TraceEvent

SKIP = ("/.venv/", "/site-packages/", "/__pycache__/")


def load_events(root: Path, trace_path: str) -> list[TraceEvent]:
    rows = json.loads(Path(trace_path).read_text())
    keep = [r for r in rows
            if not any(s in r["file"] for s in SKIP)
            and r["file"].startswith(str(root) + "/")
            and r["file"].endswith(".py")
            and Path(r["file"]).is_file()]
    return [TraceEvent(0, i, r["file"], r["function"], r["line"], r["event_type"])
            for i, r in enumerate(keep)], len(rows)


def unexplained(mapped, ir) -> int:
    synthetic = {"entry", "exit", "loop_exit", "returnsite", "callsite"}
    outgoing: dict[str, list[str]] = {}
    for edge in ir.edges:
        outgoing.setdefault(edge.src, []).append(edge.dst)
    total = 0
    for src, dst in mapped.unmatched_transitions:
        if any(dst == nxt or (ir.blocks[nxt].kind in synthetic and dst in outgoing.get(nxt, []))
               for nxt in outgoing.get(src, [])):
            continue
        total += 1
    return total


def measure(name: str, rootp: str, trace_path: str) -> dict:
    root = Path(rootp).resolve()
    events, raw = load_events(root, trace_path)
    ir0 = build_icfg(root)
    learned = learn_call_edges(events, ir0)
    sites = unresolved_sites(ir0)
    ir1 = build_icfg(root, dynamic=learned)
    m0, m1 = map_trace(events, ir0), map_trace(events, ir1)
    overlay = build_overlay(ir1, m1)
    row = {
        "app": name,
        "raw_events": raw,
        "project_events": len(events),
        "functions": len(ir0.functions),
        "blocks": len(ir0.blocks),
        "covered_blocks": len(overlay.covered_blocks),
        "frontiers": len(find_frontiers(ir1, overlay)),
        "learned": len(learned),
        "learned_hits": sum(1 for k in learned if k in sites),
        "unresolved_sites": [len(ir0.metadata["unresolved_calls"]),
                             len(ir1.metadata["unresolved_calls"])],
        "unresolved_events": [len(m0.unresolved), len(m1.unresolved)],
        "unexplained": [unexplained(m0, ir0), unexplained(m1, ir1)],
    }
    return row


TRACE_DIR = "/tmp"

APPS = [
    ("microblog", "external/microblog", "/tmp/sf_trace_microblog.json"),
    ("CTFd", "external/CTFd", "/tmp/sf_trace_ctfd.json"),
    ("realworld-django-ninja", "external/realworld-django-ninja", "/tmp/sf_trace_realworld.json"),
    ("linkding", "external/linkding", "/tmp/sf_trace_linkding.json"),
    ("healthchecks", "external/healthchecks", "/tmp/sf_trace_healthchecks.json"),
    ("django-oscar", "external/django-oscar", "/tmp/sf_trace_oscar.json"),
    ("fava", "external/fava", "/tmp/sf_trace_fava.json"),
    ("Radicale", "external/Radicale", "/tmp/sf_trace_radicale.json"),
]

if __name__ == "__main__":
    for name, path, trace in APPS:
        try:
            print(json.dumps(measure(name, path, trace), ensure_ascii=False), flush=True)
        except Exception as exc:                 # noqa: BLE001
            print(json.dumps({"app": name, "error": f"{type(exc).__name__}: {exc}"},
                             ensure_ascii=False), flush=True)
