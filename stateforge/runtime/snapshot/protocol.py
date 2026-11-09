"""protocol.py —— 控制信道原语。

拷贝自 exp/atropos_snapshot.py(_send/_recv/_pss_kb/_rss_kb/_on_alarm,冻结件),
AF_UNIX socketpair 与文件 socket 字节兼容(ADR-0001 驱动/监督两进程族)。
"""

from __future__ import annotations

import json
import os
import signal
import socket


class LeafTimeout(Exception):
    pass


_MAX_FRAME_BYTES = 1024 * 1024


def _send(sock, obj, fds=()):
    data = (json.dumps(obj) + "\n").encode()
    if len(data) > _MAX_FRAME_BYTES:
        raise ValueError("snapshot control frame exceeds size limit")
    if fds:
        # sendmsg can write only a prefix. Rights belong to that prefix and
        # must never be sent again when completing the remainder.
        sent = socket.send_fds(sock, [data], fds)
        if sent == 0:
            raise ConnectionError("snapshot control socket closed during send")
        sock.sendall(data[sent:])
    else:
        sock.sendall(data)


def _recv(sock, maxfds=4):
    if maxfds < 0:
        raise ValueError("maxfds must be nonnegative")
    data = bytearray()
    fds = []
    try:
        while True:
            # STREAM ancillary data has no application-level message boundary.
            # Reading a byte at a time avoids reading beyond a newline and
            # assigning the next frame's SCM_RIGHTS to this one. Control frames
            # are small; this also avoids per-socket buffers surviving fork.
            chunk, received, flags, _ = socket.recv_fds(sock, 1, maxfds)
            fds.extend(received)
            if flags & socket.MSG_CTRUNC or len(fds) > maxfds:
                raise ValueError("snapshot control frame has too many descriptors")
            if not chunk:
                if data or fds:
                    raise EOFError("truncated snapshot control frame")
                return None, ()
            data.extend(chunk)
            if len(data) > _MAX_FRAME_BYTES:
                raise ValueError("snapshot control frame exceeds size limit")
            if chunk == b"\n":
                obj = json.loads(data.decode())
                return obj, tuple(fds)
    except BaseException:
        # Ownership transfers only after a successfully decoded complete frame.
        for fd in fds:
            os.close(fd)
        raise


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
