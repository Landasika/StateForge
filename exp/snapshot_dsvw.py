#!/usr/bin/env python3
"""Snapshot-mode harness for the ANOTA-annotated DSVW target (L1: pre-fork).

The parent process freezes at the checkpoint: in-memory SQLite initialized,
listen socket bound. Every testcase is served by a fork()ed copy-on-write
child that handles exactly one request and then os._exit()s, so each
testcase starts from the snapshot state (Atropos-style semantics without
Nyx/KVM, which is unavailable here).

Measurements per testcase: fork cost, request round-trip (serve), reap cost.
Isolation evidence: parent RSS stability across the full matrix (including
a bounded DoS probe served by a child) and cross-testcase DB write discard.

Usage:  <anota-python> snapshot_dsvw.py [--port=65413]
"""
import json, os, statistics, sys, time
import http.server

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_dsvw
from harness_dsvw import TESTS, CWD, HOST, policies_in, request, summarize

PORT = harness_dsvw.PORT + 1
for arg in sys.argv[1:]:
    if arg.startswith("--port="):
        PORT = int(arg.split("=", 1)[1])
harness_dsvw.PORT = PORT  # keep the shared HTTP client pointed at our listener

sys.path.insert(0, "/home/landasika/icassp/exp")
os.chdir(CWD)                # fixture files (static.txt, plugin.txt) live here
import dsvw_anota as app     # annotated target module (ANOTA interpreter required)

def rss_kb(pid=None):
    with open("/proc/%s/status" % (pid or "self")) as fh:
        for line in fh:
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    return -1

def fork_serve_one(server):
    """fork a child that serves exactly one request, return (pid, fork_ms)."""
    t0 = time.perf_counter()
    pid = os.fork()
    if pid == 0:  # child: synchronous single-request service, then hard exit
        try:
            server.handle_request()
        finally:
            os._exit(0)
    return pid, (time.perf_counter() - t0) * 1e3

def reap(pid, timeout=10.0):
    t0 = time.perf_counter()
    deadline = t0 + timeout
    while True:
        waited, status = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return (time.perf_counter() - t0) * 1e3, status
        if time.perf_counter() > deadline:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            return (time.perf_counter() - t0) * 1e3, -1
        time.sleep(0.0005)

def snapshot_request(server, target, timeout=60):
    pid, fork_ms = fork_serve_one(server)
    t0 = time.perf_counter()
    error = None
    try:
        status, body = request(target, timeout=timeout)
    except Exception as ex:
        status, body, error = None, "", "%s: %s" % (type(ex).__name__, ex)
    serve_ms = (time.perf_counter() - t0) * 1e3
    reap_ms, wait_status = reap(pid)
    return status, body, dict(fork_ms=fork_ms, serve_ms=serve_ms, reap_ms=reap_ms,
                              wait_status=wait_status, error=error)

def main():
    open("/tmp/dsvw_snapshot.log", "wb").close()
    # ---- checkpoint: DB initialized, listen socket bound, state frozen ----
    app.init()  # :memory: SQLite + tables (COW-shared with all children)
    server = http.server.HTTPServer((HOST, PORT), app.ReqHandler)  # sync: no ThreadingMixIn (fork/_exit hazard)
    server.timeout = 60
    parent_rss_ckpt = rss_kb()
    print("checkpoint: pid=%d port=%d RSS=%dkB" % (os.getpid(), PORT, parent_rss_ckpt))

    rows, timings = [], []
    wall0 = time.perf_counter()
    for name, target, expect, policy, group, note in TESTS:
        status, body, tm = snapshot_request(server, target)
        hits = policies_in(body)
        if expect == "attack":
            verdict = "TP" if policy in hits else ("FN" if not hits else "TP-MISMATCH")
        else:
            verdict = "TN" if not hits else "FP"
        rows.append(dict(name=name, group=group, expect=expect, policy=policy,
                         hits=hits, status=status, verdict=verdict,
                         error=tm["error"], note=note))
        timings.append(dict(name=name, **tm))
        print("%-24s %-4s %-7s %-22s fork %.2fms serve %.2fms reap %.2fms%s"
              % (name, expect, verdict, ",".join(hits) or "-",
                 tm["fork_ms"], tm["serve_ms"], tm["reap_ms"],
                 ("  ERROR %s" % tm["error"]) if tm["error"] else ""))
    wall = time.perf_counter() - wall0

    # ---- isolation probe 1: child DB writes must not survive the child ----
    snapshot_request(server, "/?comment=probe123")   # INSERT inside a child, child exits
    _, listing, _ = snapshot_request(server, "/?comment=")
    isolation_ok = "probe123" not in listing
    print("state isolation (comment write discarded by snapshot): %s" % ("OK" if isolation_ok else "FAIL"))

    # ---- isolation probe 2: bounded DoS in a child, parent untouched ----
    status, _, _ = snapshot_request(server, "/?size=10000")
    child_peak = rss_kb()  # parent RSS after serving the memory-heavy request in a child
    print("DoS probe (?size=10000): status=%s, parent RSS after=%dkB (checkpoint %dkB)"
          % (status, child_peak, parent_rss_ckpt))

    counts = summarize(rows)
    forks = [t["fork_ms"] for t in timings]
    serves = [t["serve_ms"] for t in timings]
    reaps = [t["reap_ms"] for t in timings]
    overheads = [t["fork_ms"] + t["reap_ms"] for t in timings]
    print("\nsnapshot confusion matrix:", json.dumps(counts))
    print("fork  median %.3f ms (min %.3f, max %.3f)" % (statistics.median(forks), min(forks), max(forks)))
    print("serve median %.2f ms (min %.2f, max %.2f)" % (statistics.median(serves), min(serves), max(serves)))
    print("reap  median %.3f ms (min %.3f, max %.3f)" % (statistics.median(reaps), min(reaps), max(reaps)))
    print("recovery overhead (fork+reap) median %.3f ms (min %.3f) | throughput %.2f testcase/s (incl. request handling)"
          % (statistics.median(overheads), min(overheads), len(TESTS) / wall))

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "snapshot_results.json"), "w") as fh:
        json.dump(dict(mode="snapshot", port=PORT, parent_rss_ckpt_kb=parent_rss_ckpt,
                       parent_rss_after_kb=child_peak, state_isolation_ok=isolation_ok,
                       wall_seconds=wall, rows=rows, counts=counts, timings=timings,
                       fork_ms=dict(median=statistics.median(forks), min=min(forks), max=max(forks)),
                       serve_ms=dict(median=statistics.median(serves), min=min(serves), max=max(serves)),
                       reap_ms=dict(median=statistics.median(reaps), min=min(reaps), max=max(reaps)),
                       overhead_fork_plus_reap_ms=dict(median=statistics.median(overheads), min=min(overheads)),
                       throughput_testcases_per_sec=len(TESTS) / wall), fh, indent=1)
    print("results -> exp/results/snapshot_results.json")

if __name__ == "__main__":
    main()
