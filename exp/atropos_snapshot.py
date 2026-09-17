#!/usr/bin/env python3
"""Atropos-style snapshot engine for ANOTA targets (process-level Nyx stand-in).

Ports the snapshot strategies of ATROPOS (USENIX Security'24, §3.3/§4) to
process level, since /dev/kvm (Nyx) is unavailable here:

  * base strategy   : one snapshot after full initialization (DB + listen
                      socket); restore after every served input
  * reset-every-n   : restore only after every n inputs so stateful request
                      chains accumulate state within one snapshot epoch
  * timeout restore : a leaf exceeding its budget is SIGKILLed and the
                      checkpoint restored (paper: 1s per-input timeout)
  * multi-depth     : nested checkpoints (after login / after resource
                      creation, ...) restored incrementally without replaying
                      the chain (Nyx multi-snapshot idea, fork()-flavoured)

Process tree:  driver -> root(depth0) -> ckpt(depth1) -> ckpt(depth2) -> ...
Restore = fork() a disposable "leaf" from the frozen checkpoint (COW). Every
process inherits the listen socket; only leaves ever accept() connections,
so checkpoint state can never be polluted by test traffic.
"""
import importlib, json, os, signal, socket, sys, time
import http.server


class LeafTimeout(Exception):
    pass


class CountingHTTPServer(http.server.HTTPServer):
    """HTTPServer with a per-leaf request quota (reset-every-n semantics)."""
    quota = None
    served = 0

    def process_request(self, request, client_address):
        super().process_request(request, client_address)
        self.served += 1


# ---- control-channel protocol (one JSON line per message, fds in ancilla) --

def _send(sock, obj, fds=()):
    data = (json.dumps(obj) + "\n").encode()
    if fds:
        socket.send_fds(sock, [data], fds)
    else:
        sock.sendall(data)

def _recv(sock, maxfds=4):
    data, fds, _, _ = socket.recv_fds(sock, 65536, maxfds)
    if not data:
        return None, ()
    return json.loads(data.decode()), tuple(fds)

def _pss_kb(pid):
    try:
        with open("/proc/%d/smaps_rollup" % pid) as fh:
            for line in fh:
                if line.startswith("Pss:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return -1

def _rss_kb(pid):
    with open("/proc/%d/status" % pid) as fh:
        for line in fh:
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    return -1

def _on_alarm(signum, frame):
    raise LeafTimeout()


def control_loop(server, ctrl, name):
    """Frozen checkpoint: never touches app state, only forks leaves."""
    signal.signal(signal.SIGALRM, _on_alarm)
    while True:
        msg, _ = _recv(ctrl)
        if msg is None:
            break
        cmd = msg.get("cmd")
        if cmd == "serve":
            _handle_serve(server, msg, ctrl)
        elif cmd == "freeze_child":
            _handle_freeze(server, msg, ctrl)
        elif cmd == "rss":
            _send(ctrl, {"evt": "rss", "name": name, "pid": os.getpid(),
                         "pss_kb": _pss_kb(os.getpid()), "rss_kb": _rss_kb(os.getpid())})
        elif cmd == "ping":
            _send(ctrl, {"evt": "pong", "name": name})
        elif cmd == "exit":
            break
    os._exit(0)


def _handle_serve(server, msg, ctrl):
    """Atropos restore: fork a leaf that serves `quota` inputs, then dies."""
    quota = int(msg.get("quota", 1))
    budget = msg.get("budget")           # seconds or None (timeout restore)
    rcount, wcount = os.pipe()           # leaf reports its served count here
    server.quota, server.served = quota, 0
    t0 = time.perf_counter()
    pid = os.fork()
    if pid == 0:                          # leaf: disposable serving copy
        try:
            while server.served < quota:
                server.handle_request()
        finally:
            os.close(rcount)
            try:
                os.write(wcount, b"%d" % server.served)
            except OSError:
                pass
            os.close(wcount)
            os._exit(0)
    os.close(wcount)
    fork_ms = (time.perf_counter() - t0) * 1e3
    _send(ctrl, {"evt": "leaf_forked", "fork_ms": fork_ms, "pid": pid})
    timed_out, t0 = False, time.perf_counter()
    if budget is not None:
        signal.setitimer(signal.ITIMER_REAL, float(budget))
    try:
        _, status = os.waitpid(pid, 0)
    except LeafTimeout:
        signal.setitimer(signal.ITIMER_REAL, 0)
        timed_out = True
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _, status = os.waitpid(pid, 0)
    finally:
        if budget is not None:
            signal.setitimer(signal.ITIMER_REAL, 0)
    raw = b""                             # leaf's own count (COW-private above)
    while True:
        chunk = os.read(rcount, 16)
        if not chunk:
            break
        raw += chunk
    os.close(rcount)
    _send(ctrl, {"evt": "done", "reap_ms": (time.perf_counter() - t0) * 1e3,
                 "served": int(raw or b"0"), "timed_out": timed_out, "status": status})


def _handle_freeze(server, msg, ctrl):
    """Nested checkpoint: fork a child that serves the build-up chain itself,
    then freezes as a new restore point. Its control fd is passed back so the
    driver can restore to this depth without replaying the chain."""
    name, build, depth = msg["name"], msg.get("build", []), msg.get("depth")
    parent_end, child_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    t0 = time.perf_counter()
    pid = os.fork()
    if pid == 0:
        parent_end.close()
        for _ in build:                   # state build-up runs in the child,
            server.handle_request()       # never in the freezing parent
        _send(child_end, {"evt": "frozen", "name": name})
        control_loop(server, child_end, name)
        os._exit(0)
    child_end.close()
    fork_ms = (time.perf_counter() - t0) * 1e3
    _send(ctrl, {"evt": "checkpoint_forked", "name": name, "pid": pid,
                 "fork_ms": fork_ms}, fds=[parent_end.fileno()])
    parent_end.close()


def spawn_root(app_module_name, cwd, host, port, log_path):
    """Fork the depth-0 checkpoint: imports the app under the ANOTA
    interpreter, runs init() (in-memory DB), binds the listen socket."""
    parent_end, child_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    pid = os.fork()
    if pid == 0:
        parent_end.close()
        log = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        os.dup2(log, 1)
        os.dup2(log, 2)
        os.chdir(cwd)
        t0 = time.perf_counter()
        app = importlib.import_module(app_module_name)
        app.init()                        # Opcache-equivalent: modules+pyc warm
        srv = CountingHTTPServer((host, port), app.ReqHandler)
        srv.timeout = None
        boot_ms = (time.perf_counter() - t0) * 1e3
        _send(child_end, {"evt": "ready", "pid": os.getpid(),
                          "boot_ms": boot_ms, "port": port})
        control_loop(srv, child_end, "root")
        os._exit(0)
    child_end.close()
    msg, _ = _recv(parent_end)
    if msg.get("evt") != "ready":
        raise RuntimeError("root checkpoint failed: %r" % msg)
    return pid, parent_end, msg
