"""supervisor.py —— 快照监督进程(ANOTA 解释器内运行;stdlib+werkzeug+目标,无 pydantic)。

用法:
  <anota-python> -m stateforge.runtime.snapshot.supervisor \
      --target vulnshop --sock /tmp/sf.sock --port 5050 [--guards 0] [--db path]

拓扑(ADR-0001):驱动进程(系统 py3.13)经 AF_UNIX 文件 socket 连到本监督进程;
监督进程 = depth-0 检查点(导入目标 + init + 绑端口 + L2 首模板),control_loop
接管。母本 exp/atropos_snapshot.py:spawn_root(同进程 fork)改造为跨进程 accept。
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from pathlib import Path

from werkzeug.serving import BaseWSGIServer

from stateforge.runtime.snapshot import engine
from stateforge.runtime.snapshot.l2_sqlite import L2Handle, L2Sqlite
from stateforge.runtime.snapshot.protocol import _send


class CountingWSGIServer(BaseWSGIServer):
    """带每叶请求配额的 WSGI server(threaded=False:fork-per-leaf 硬约束)。"""

    quota = None
    served = 0

    def process_request(self, request, client_address):
        super().process_request(request, client_address)
        self.served += 1


def build_app(args):
    if args.target == "vulnshop":
        sys.path.insert(0, str(Path(__file__).parents[2]))
        from stateforge.targets.vulnshop import app as vshop

        return vshop.create_app(db_path=args.db, guards=args.guards, init_db=True), args.db
    if args.target == "vampi":
        sys.path.insert(0, str(Path(__file__).parents[2]))
        from stateforge.targets.vampi import adapter

        flask_app = adapter.make_app(vulnerable=args.guards, token_ttl=600)
        return flask_app, str(adapter.VAMPI_DIR / "database" / "database.db")
    raise SystemExit(f"unknown target: {args.target}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True)
    ap.add_argument("--sock", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--guards", type=int, default=0)
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    t0 = time.perf_counter()
    app, db_path = build_app(args)
    server = CountingWSGIServer(args.host, args.port, app)  # port=0 → 内核随机分配
    server.timeout = None

    l2 = None
    if db_path:
        # PR-02:fork-COW 引擎路径保持位级冻结语义(capture="copy");
        # WAL 目标在该模式下 fail-closed(l2_sqlite SnapshotConsistencyError
        # 于首模板建立时显式报错)——引擎路径暂不支持 WAL,不静默降级。
        l2 = L2Handle(L2Sqlite(db_path, capture=L2Sqlite.CAPTURE_COPY), "root")
        l2.restore()                       # depth-0:先按模板还原(幂等),再冻结首模板
        l2.snapshot_template("root")       # init 后的干净态

    if os.path.exists(args.sock):
        os.unlink(args.sock)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(args.sock)
    listener.listen(1)

    boot_ms = (time.perf_counter() - t0) * 1e3
    conn, _ = listener.accept()            # 驱动进程连入
    _send(conn, {"evt": "ready", "pid": os.getpid(),
                 "boot_ms": boot_ms, "port": server.server_port, "db": db_path})
    engine.control_loop(server, conn, "root", l2=l2)


if __name__ == "__main__":
    main()
