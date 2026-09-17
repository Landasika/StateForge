#!/usr/bin/env python3
"""Second-order vulnerability detection on the ANOTA-annotated DSVW target.

The target gains a second-order flaw (DSVW style): `?comment=<path>` stores an
attacker-controlled string in the DB; `?profile=<id>` later reads that stored
value and uses it as a file path (arbitrary file read). The request that
reaches the sink carries only an innocent integer id -- the payload lives in
the database, so per-request taint reasoning cannot see it.

The oracle is an ANOTA_EXECUTION.BLOCK annotation at the *use site* (sink-side
property: resolved path must stay inside the web root), so detection needs the
*stored* state to be present: that is what the Atropos-style snapshot layer
controls. This script contrasts three modes:

  baseline        long-running server, state shared across requests
  snapshot/input  restore after every input (Atropos base strategy)
  snapshot/epoch  restore every n=4 inputs (chain lives in one epoch)

Expected: baseline TP, per-input MISS (chain broken, no false positive),
epoch TP with full isolation.

Usage:  <anota-python> second_order.py
"""
import json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_dsvw
from harness_dsvw import CWD, q, policies_in, request, start_server, wait_ready
import atropos_snapshot as eng

BASE_PORT, SNAP_PORT = 65415, 65416
LOG = "/tmp/dsvw_second_order.log"

STEPS = [  # (label, path, kind)  ids assume the comments table starts empty
    ("store_benign", "/?comment=" + q("static.txt"), "store"),
    ("store_attack", "/?comment=" + q("../../../../../../etc/passwd"), "store"),
    ("use_benign",   "/?profile=1", "use"),   # stored in-root file -> must pass
    ("use_attack",   "/?profile=2", "use"),   # stored traversal -> must be caught
]

def classify(body):
    hits = policies_in(body)
    if "profile_web_root" in hits:
        return "TP", hits
    if hits:
        return "FP", hits
    if "No avatar set" in body:
        return "MISS(empty state)", hits
    return "TN", hits

def drive(step_responses):
    """step_responses: list of (status, body, error) for STEPS in order."""
    rows = []
    for (label, path, kind), (status, body, error) in zip(STEPS, step_responses):
        if kind == "store":
            verdict, hits = "STORED", []
        else:
            verdict, hits = classify(body)
        rows.append(dict(step=label, status=status, verdict=verdict, hits=hits,
                         error=error))
    return rows

def mode_baseline():
    harness_dsvw.PORT = BASE_PORT
    open(LOG, "wb").close()
    proc = start_server(BASE_PORT, LOG)
    wait_ready(BASE_PORT)
    try:
        return drive([tuple(request(path)) + (None,) for _, path, _ in STEPS])
    finally:
        proc.terminate()
        proc.wait(timeout=5)

def _serve(root_ctrl, paths):
    eng._send(root_ctrl, {"cmd": "serve", "quota": len(paths), "budget": None})
    assert eng._recv(root_ctrl)[0]["evt"] == "leaf_forked"
    responses = []
    for p in paths:
        try:
            status, body = request(p, timeout=30)
            responses.append((status, body, None))
        except Exception as ex:
            responses.append((None, "", "%s: %s" % (type(ex).__name__, ex)))
    assert eng._recv(root_ctrl)[0]["evt"] == "done"
    return responses

def mode_snapshot(per_epoch):
    harness_dsvw.PORT = SNAP_PORT
    open(LOG, "wb").close()
    root_pid, root_ctrl, _ = eng.spawn_root("dsvw_anota", CWD, "127.0.0.1", SNAP_PORT, LOG)
    try:
        if per_epoch == 1:                      # restore after every input
            out = [ _serve(root_ctrl, [path])[0] for _, path, _ in STEPS ]
        else:                                   # whole chain in one epoch
            out = _serve(root_ctrl, [path for _, path, _ in STEPS])
        return drive(out)
    finally:
        eng._send(root_ctrl, {"cmd": "exit"})
        root_ctrl.close()
        try:
            os.waitpid(root_pid, 0)
        except ChildProcessError:
            pass

def main():
    results = {}
    for name, fn in (("baseline", mode_baseline),
                     ("snapshot_per_input", lambda: mode_snapshot(1)),
                     ("snapshot_epoch_n4", lambda: mode_snapshot(4))):
        rows = fn()
        use_rows = [r for r in rows if r["step"].startswith("use")]
        detected = any(r["verdict"] == "TP" for r in use_rows)
        false_pos = any(r["verdict"] == "FP" for r in use_rows)
        results[name] = dict(rows=rows, second_order_detected=detected,
                             false_positive=false_pos)
        print("%-18s %s" % (name, "-" * 52))
        for r in rows:
            print("    %-12s -> %-4s %-6s %s%s" % (r["step"], r["status"],
                  r["verdict"], ",".join(r["hits"]) or "-",
                  ("  ERROR %s" % r["error"]) if r["error"] else ""))
        print("    => second-order detected: %s | false positive: %s\n"
              % (detected, false_pos))

    print("summary: baseline=%(baseline)s, per-input=%(snapshot_per_input)s, "
          "epoch(n=4)=%(snapshot_epoch_n4)s" % {
              k: ("DETECT" if v["second_order_detected"] else
                  ("FP" if v["false_positive"] else "MISS"))
              for k, v in results.items()})

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results",
                       "second_order_results.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as fh:
        json.dump(results, fh, indent=1)
    print("results -> exp/results/second_order_results.json")

if __name__ == "__main__":
    main()
