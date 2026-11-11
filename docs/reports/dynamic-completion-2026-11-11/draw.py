"""Render one target's ICFG before and after a short fuzzing run.

Three things get marked, and each has a different source, so the picture
never conflates them:

* **coverage** -- blocks the trace actually entered, from ``map_trace``.
  Green, with a hit count.
* **dynamic edges** -- call sites the trace settled that the static pass left
  open, from ``learn_call_edges``.  Blue dashed.  These are *additions*: the
  static edge is still there underneath.
* **session access** -- blocks that read or write the session, from
  ``extract_session_accesses``.  This one is static (it comes from the AST, not
  the run), so it is drawn as a border, not a fill, and it survives on blocks
  that were never executed.  That combination is the interesting part: a
  session-mutating block that is orange-and-grey is a state transition the
  fuzzer has not taken yet.

Session-marked blocks get a rank hint so they do not scatter, and the dot file
is written next to a ``.svg`` because a graph of any real size needs a viewer
that can zoom.

Usage::

    python docs/reports/dynamic-completion-2026-11-11/draw.py microblog
    python docs/reports/dynamic-completion-2026-11-11/draw.py microblog --requests 40
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from stateforge.evaluation.viz.coverage_dot import coverage_dot

APP = REPO / "stateforge" / "targets" / "apps20"
EXTERNAL = REPO / "external"


def _target_root(label: str) -> Path:
    """The application tree for *label*.

    ``external/`` holds the real projects; a few labels map to a subdirectory
    (``benchmarkpython``) rather than the project root.
    """
    direct = EXTERNAL / label
    if direct.is_dir():
        return direct.resolve()
    for candidate in sorted(EXTERNAL.iterdir()):
        if candidate.is_dir() and candidate.name.lower() == label.lower():
            return candidate.resolve()
    raise SystemExit(f"no source tree for {label!r} under {EXTERNAL}")


def build_marks(ir, events):
    """Return (covered, hits, dynamic) for one trace.

    ``dynamic`` is computed against *ir* as built -- i.e. the sites that were
    still open -- so the blue edges are exactly the ones the run added.  Doing
    it this way means the two pictures differ only where the run taught us
    something.
    """
    from stateforge.analysis.dynamic.callgraph import learn_call_edges
    from stateforge.runtime.mapper import map_trace

    mapped = map_trace(events, ir)
    covered = set(mapped.blocks)
    hits = dict(mapped.block_hits)
    learned = learn_call_edges(events, ir)
    return covered, hits, learned


def render(ir, covered, hits, new_edges, session_blocks, path: Path,
           focus: str | None = None, around: str | None = None,
           only_new: bool = False) -> Path:
    """Thin wrapper over the package renderer, which owns the dot format.

    Keeping the markup rules in ``stateforge.evaluation.viz.coverage_dot``
    means the picture the report shows is the same one ``stateforge viz``
    produces; a local copy would drift.
    """
    feedback = {"covered_blocks": sorted(covered), "block_hits": hits,
                "frontiers": []}
    return coverage_dot(ir, feedback, path, session_blocks=session_blocks,
                        learned_edges=new_edges, function=focus,
                        around=around, only_learned=only_new)


def to_svg(dot_path: Path) -> Path | None:
    svg = dot_path.with_suffix(".svg")
    try:
        subprocess.run(["dot", "-Tsvg", str(dot_path), "-o", str(svg)],
                       check=True, capture_output=True, timeout=300)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return svg


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("label")
    ap.add_argument("--requests", type=int, default=30,
                    help="how many harness requests to include in the run")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "graphs"))
    ap.add_argument("--new-edges-only", action="store_true",
                    help="draw only the blocks touched by learned edges")
    ap.add_argument("--around-block", type=int, default=None, metavar="LINE",
                    help="draw the session block on LINE plus its neighbours")
    ap.add_argument("--focus", default=None,
                    help="limit to one function (qname or trailing name)")
    args = ap.parse_args()

    from stateforge.analysis.static.icfg import build_icfg
    from stateforge.fuzzing.dynamic_collect import collect_target
    from stateforge.state.session_access import extract_session_accesses

    root = _target_root(args.label)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    ir = build_icfg(root)
    # extract_session_accesses walks the whole tree, so an in-tree virtualenv
    # contributes session sites too (microblog's first hit was an aiosmtpd test
    # file).  Drop the dependency roots first, exactly as the collector does --
    # otherwise the marks land on installed code the graph has no nodes for.
    from stateforge.analysis.dynamic.collect import app_exclusions
    banned = tuple(str(p.resolve()) + os.sep for p in app_exclusions(root))
    session = [a for a in extract_session_accesses(root, ir)
               if not str(Path(a.source_file).resolve()).startswith(banned)]
    session_blocks = {a.block_id for a in session if a.block_id}
    print(f"[{args.label}] functions={len(ir.functions)} blocks={len(ir.blocks)} "
          f"edges={len(ir.edges)} unresolved={len(ir.metadata.get('unresolved_calls', []))}")
    print(f"[{args.label}] session access sites={len(session)} "
          f"on blocks={len(session_blocks)}")

    # -- before: nothing has run
    around = args.around_block
    if around is not None:
        # Accept a line number: pick the session block on that line.
        matches = [a.block_id for a in session
                   if a.block_id and a.source_line == around]
        if not matches:
            raise SystemExit(f"no session access on line {around}")
        around = matches[0]
        print(f"[{args.label}] around block={around}")
    suffix = (f"-{args.focus.rsplit('.', 1)[-1]}" if args.focus else
              (f"-L{args.around_block}" if args.around_block else ""))
    before = render(ir, set(), {}, {}, session_blocks,
                    out_dir / f"{args.label}{suffix}-before.dot", args.focus, around)
    print(f"[{args.label}] before -> {before}")

    # -- run
    result = collect_target(args.label, ir, root)
    if result.error:
        print(f"[{args.label}] harness error: {result.error}")
    print(f"[{args.label}] events={result.event_count} learned={len(result.learned)}")

    covered, hits, learned = build_marks(ir, result.events)
    after = render(ir, covered, hits, set(), session_blocks,
                   out_dir / f"{args.label}{suffix}-after.dot", args.focus, around)
    print(f"[{args.label}] after  -> {after}  covered_blocks={len(covered)}")

    # -- the graph with dynamic edges folded in.  The *difference* against the
    # static edge set is what gets painted blue: this is exactly the edge the
    # run added, with no pairing heuristics in the way.
    ir2 = build_icfg(root, dynamic=result.learned)
    static_edges = {(e.src, e.dst) for e in ir.edges}
    new_edges = {(e.src, e.dst) for e in ir2.edges} - static_edges
    covered2, hits2, _ = build_marks(ir2, result.events)
    merged = render(ir2, covered2, hits2, new_edges, session_blocks,
                    out_dir / f"{args.label}{suffix}-merged.dot", args.focus, around)
    print(f"[{args.label}] new edges={len(new_edges)}")

    if args.new_edges_only:
        # Learned edges only: no static edges, no unrelated blocks.
        only = render(ir2, covered2, hits2, new_edges, session_blocks,
                      out_dir / f"{args.label}-LEARNED-only.dot", None, None, True)
        print(f"[{args.label}] learned-only -> {only}")
    print(f"[{args.label}] merged -> {merged}  "
          f"blocks={len(ir2.blocks)} unresolved={len(ir2.metadata.get('unresolved_calls', []))}")

    for dot in (before, after, merged):
        svg = to_svg(dot)
        if svg:
            print(f"[{args.label}] svg    -> {svg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
