#!/usr/bin/env python3
"""Driver: applies the Atropos snapshot strategies to the ANOTA-annotated
DSVW target and benchmarks them (multi-depth restore, reset-every-n,
timeout-restore, stateful-chain visibility, oracle matrix from a restored
checkpoint).

Usage:  <anota-python> snapshot_atropos.py
"""
import json, os, socket, statistics, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_dsvw
from harness_dsvw import TESTS, CWD, HOST, PICKLE_RCE, policies_in, request, summarize
import atropos_snapshot as eng

PORT = 65414
harness_dsvw.PORT = PORT                 # shared HTTP client follows our listener
LOG = "/tmp/dsvw_atropos.log"
BENIGN_MIX = ["/?tmpl=hello%20world", "/?path=static.txt", "/?name=dian",
              "/?charset=utf8", "/"]

class Checkpoint:
    def __init__(self, ctrl, pid, name):
        self.ctrl, self.pid, self.name = ctrl, pid, name
        self.ctrl.settimeout(60)

    # -- Atropos base strategy: restore-per-input; quota>1 = reset-every-n --
    def serve(self, paths, budget=None, http_timeout=60):
        """One leaf serves len(paths) inputs, then dies (state discarded)."""
        eng._send(self.ctrl, {"cmd": "serve", "quota": len(paths), "budget": budget})
        leaf = eng._recv(self.ctrl)[0]
        assert leaf["evt"] == "leaf_forked"
        responses = []
        for p in paths:
            error = None
            try:
                status, body = request(p, timeout=http_timeout)
            except Exception as ex:
                status, body, error = None, "", "%s: %s" % (type(ex).__name__, ex)
            responses.append((status, body, error))
        done = eng._recv(self.ctrl)[0]
        assert done["evt"] == "done"
        return dict(fork_ms=leaf["fork_ms"], reap_ms=done["reap_ms"],
                    served=done["served"], timed_out=done["timed_out"],
                    responses=responses)

    def freeze_child(self, name, build_paths, depth=None):
        eng._send(self.ctrl, {"cmd": "freeze_child", "name": name,
                              "build": build_paths, "depth": depth})
        msg, fds = eng._recv(self.ctrl)
        assert msg["evt"] == "checkpoint_forked", msg
        child_ctrl = socket.socket(fileno=fds[0])
        for p in build_paths:            # consumed by the build child
            request(p, timeout=30)
        frozen = eng._recv(child_ctrl)[0]
        assert frozen["evt"] == "frozen", frozen
        return Checkpoint(child_ctrl, msg["pid"], name)

    def info(self):
        eng._send(self.ctrl, {"cmd": "rss"})
        return eng._recv(self.ctrl)[0]

    def exit(self):
        try:
            eng._send(self.ctrl, {"cmd": "exit"})
        except OSError:
            pass
        self.ctrl.close()

def stats(samples):
    return dict(median=round(statistics.median(samples), 3),
                min=round(min(samples), 3), max=round(max(samples), 3))

def main():
    results = dict(target="dsvw_anota", port=PORT)
    open(LOG, "wb").close()

    t0 = time.perf_counter()
    root_pid, root_ctrl, ready = eng.spawn_root("dsvw_anota", CWD, HOST, PORT, LOG)
    results["root_bootstrap_ms"] = round(ready["boot_ms"], 1)
    print("root checkpoint (depth0): boot %.1f ms (interpreter import + DB init + bind)"
          % ready["boot_ms"])
    root = Checkpoint(root_ctrl, root_pid, "root")

    # ---- nested checkpoints: comment state accumulates with depth ----------
    c1 = root.freeze_child("after_comment_1", ["/?comment=atropos_seed_one"], depth=1)
    c2 = c1.freeze_child("after_comment_2", ["/?comment=atropos_seed_two"], depth=2)
    print("checkpoints: root -> %s(pid %d) -> %s(pid %d)" %
          (c1.name, c1.pid, c2.name, c2.pid))
    assert c1.info()["pid"] != c2.info()["pid"]

    # ---- 1. restore latency by depth (Nyx: restore cost independent of chain)
    # NOTE: fork_ms = restore proper; reap_ms is measured from just after the
    # fork (checkpoint blocks in waitpid), so it *includes* the request window.
    print("\n[1] restore latency by depth (30 restores each, GET /?tmpl=probe)")
    results["restore_by_depth"] = {}
    for depth, ck in ((0, root), (1, c1), (2, c2)):
        f, r = [], []
        for _ in range(30):
            out = ck.serve(["/?tmpl=restore_probe"])
            f.append(out["fork_ms"]); r.append(out["reap_ms"])
        row = dict(fork_ms=stats(f), cycle_ms=stats([a + b for a, b in zip(f, r)]),
                   restore_rate_per_sec=round(1000.0 / statistics.median(f), 1))
        results["restore_by_depth"]["depth%d" % depth] = row
        print("    depth%d: restore(fork) %.3f ms (~%s/s), full cycle incl. request %.3f ms"
              % (depth, row["fork_ms"]["median"], row["restore_rate_per_sec"],
                 row["cycle_ms"]["median"]))

    # ---- 2. reset-every-n: restore only after every n inputs ----------------
    print("\n[2] reset-every-n: 50 benign inputs, one leaf per n-input epoch")
    results["reset_every_n"] = {}
    for n in (1, 10, 50):
        paths = [BENIGN_MIX[i % len(BENIGN_MIX)] for i in range(50)]
        t0 = time.perf_counter()
        forks, served_total = 0, 0
        for i in range(0, 50, n):
            out = root.serve(paths[i:i + n])
            forks += 1
            served_total += out["served"]
        wall = time.perf_counter() - t0
        results["reset_every_n"][str(n)] = dict(wall_s=round(wall, 3), forks=forks,
                                                served=served_total,
                                                ms_per_input=round(wall / 50 * 1000, 2))
        print("    n=%2d: %2d restores for 50 inputs, wall %.2f s (~%.2f ms/input)"
              % (n, forks, wall, wall / 50 * 1000))
    assert served_total == 50

    # ---- 3. timeout restore (paper: restore after 1s timeout) --------------
    print("\n[3] timeout restore (budget 0.4s vs pickle payload that pings for ~1s)")
    out = root.serve(["/?object=" + PICKLE_RCE], budget=0.4, http_timeout=5)
    killed = out["timed_out"] and out["responses"][0][2] is not None
    after = root.serve(["/"])
    ok = after["responses"][0][0] == 200
    results["timeout_restore"] = dict(killed=bool(killed), system_recovered=bool(ok))
    print("    leaf killed by watchdog: %s; checkpoint healthy afterwards: %s (GET / -> %s)"
          % (killed, ok, after["responses"][0][0]))

    # ---- 4. stateful chain visibility (why Atropos needs reset-every-n) ----
    print("\n[4] second-order chain: insert probe then list comments")
    root.serve(["/?comment=probe_A"])                     # dies with its leaf
    listed = root.serve(["/?comment="])["responses"][0][1]
    per_input_visible = "probe_A" in listed
    chained = root.serve(["/?comment=probe_B", "/?comment="])  # same leaf epoch
    epoch_visible = "probe_B" in chained["responses"][1][1]
    results["chain_visibility"] = dict(restore_per_input=per_input_visible,
                                       reset_every_n_epoch=epoch_visible)
    print("    restore-per-input: probe visible = %s (expected False: write dies with leaf)"
          % per_input_visible)
    print("    reset-every-n(2):  probe visible = %s (expected True: chain in one epoch)"
          % epoch_visible)
    assert not per_input_visible and epoch_visible

    # ---- 5. ANOTA oracle matrix from a restored (depth-1) checkpoint -------
    print("\n[5] full ANOTA detection matrix served from checkpoint depth1")
    rows = []
    for name, target, expect, policy, group, note in TESTS:
        out = c1.serve([target])
        status, body, error = out["responses"][0]
        hits = policies_in(body)
        verdict = ("TP" if policy in hits else "FN") if expect == "attack" \
            else ("TN" if not hits else "FP")
        rows.append(dict(name=name, expect=expect, policy=policy, hits=hits,
                         status=status, verdict=verdict))
    counts = dict(TP=sum(r["verdict"] == "TP" for r in rows),
                  FN=sum(r["verdict"] == "FN" for r in rows),
                  FP=sum(r["verdict"] == "FP" for r in rows),
                  TN=sum(r["verdict"] == "TN" for r in rows))
    results["matrix_from_depth1"] = dict(counts=counts, rows=rows)
    print("    counts: %s (parity with baseline/snapshot harness: 11/0/0/9)" % counts)

    # ---- 6. memory of the checkpoint tree ----------------------------------
    print("\n[6] checkpoint tree memory (PSS = proportional set size, shared COW)")
    results["tree_memory"] = {}
    for ck in (root, c1, c2):
        info = ck.info()
        results["tree_memory"][ck.name] = dict(pid=info["pid"],
                                               rss_kb=info["rss_kb"], pss_kb=info["pss_kb"])
        print("    %-16s pid=%d RSS=%dkB PSS=%dkB" %
              (ck.name, info["pid"], info["rss_kb"], info["pss_kb"]))

    for ck in (c2, c1, root):
        ck.exit()
    try:
        os.waitpid(root_pid, 0)
    except ChildProcessError:
        pass

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results",
                            "atropos_snapshot_results.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump(results, fh, indent=1)
    print("\nresults -> exp/results/atropos_snapshot_results.json")

if __name__ == "__main__":
    main()
