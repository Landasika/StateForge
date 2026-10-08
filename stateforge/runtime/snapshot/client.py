"""client.py —— 驱动侧检查点句柄(SF2 §3 契约;母本 exp/snapshot_atropos.py:Checkpoint)。

  CheckpointHandle.serve(requests, budget)      一个叶服务 len(requests) 个输入
  CheckpointHandle.freeze_child(name, build)    嵌套检查点(build 由驱动发 HTTP)
契约:serve/freeze 期间驱动发送的请求数必须 = quota/len(build),否则 hang。
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from stateforge.runtime.snapshot.protocol import _recv, _send


@dataclass
class ServeMetrics:
    fork_ms: float
    reap_ms: float
    served: int
    timed_out: bool
    responses: list = field(default_factory=list)


class CheckpointHandle:
    def __init__(self, ctrl: socket.socket, pid: int, name: str, base_url: str):
        self.ctrl, self.pid, self.name, self.base_url = ctrl, pid, name, base_url
        self.ctrl.settimeout(120)

    # ---------------------------------------------------------------- 连接

    @classmethod
    def connect(cls, sock_path: str, host: str = "127.0.0.1", port: int | None = None,
                deadline: float = 20.0):
        t0 = time.perf_counter()
        while True:
            try:
                ctrl = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                ctrl.connect(sock_path)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if time.perf_counter() - t0 > deadline:
                    raise
                time.sleep(0.1)
        ctrl.settimeout(deadline)
        msg, _ = _recv(ctrl)
        if msg is None or msg.get("evt") != "ready":
            raise RuntimeError(f"supervisor not ready: {msg!r}")
        port = port or msg["port"]
        return cls(ctrl, msg["pid"], "root", f"http://{host}:{port}"), msg

    # ---------------------------------------------------------------- 操作

    def _request_once(self, req: dict) -> dict:
        """一请求一连接(连接池 keep-alive 会让叶的 quota 计数失真——按连接计数的
        process_request 需要每个请求独占一条连接,与 exp 母本的 per-request
        http.client 同语义)。"""
        with httpx.Client(base_url=self.base_url, timeout=30.0) as client:
            resp = client.request(
                req.get("method", "GET"), req["route"],
                json=req.get("json"), headers=req.get("headers"),
            )
        try:
            body: Any = resp.json()
        except Exception:  # noqa: BLE001
            body = resp.text
        return {"status": resp.status_code, "body": body, "error": None}

    def serve(self, requests, budget: float | None = None, quota: int | None = None) -> ServeMetrics:
        """requests: 静态 list[dict] 或动态 callable(i, responses) -> dict。

        动态形态用于"响应驱动"的链(如 login 后才知道 token);此时必须显式给
        quota(引擎在 fork 前就要知道配额)。
        """
        if callable(requests):
            assert quota is not None, "动态请求流必须显式给 quota"
        else:
            quota = quota if quota is not None else len(requests)
        _send(self.ctrl, {"cmd": "serve", "quota": quota, "budget": budget})
        leaf = _recv(self.ctrl)[0]
        assert leaf["evt"] == "leaf_forked", leaf
        responses = []
        for i in range(quota):
            req = requests(i, responses) if callable(requests) else requests[i]
            try:
                responses.append(self._request_once(req))
            except Exception as ex:  # noqa: BLE001
                responses.append({"status": None, "body": "", "error": str(ex)})
        done = _recv(self.ctrl)[0]
        assert done["evt"] == "done", done
        return ServeMetrics(leaf["fork_ms"], done["reap_ms"], done["served"],
                            done["timed_out"], responses)

    def freeze_child(self, name: str, build: list[dict]) -> "CheckpointHandle":
        _send(self.ctrl, {"cmd": "freeze_child", "name": name, "build": len(build)})
        msg, fds = _recv(self.ctrl)
        assert msg["evt"] == "checkpoint_forked", msg
        child_ctrl = socket.socket(fileno=fds[0])
        child = CheckpointHandle(child_ctrl, msg["pid"], name, self.base_url)
        for req in build:                  # build 链由冻结中的子进程自己服务
            self._request_once(req)
        frozen = _recv(child_ctrl)[0]
        assert frozen["evt"] == "frozen", frozen
        return child

    def info(self) -> dict:
        _send(self.ctrl, {"cmd": "rss"})
        return _recv(self.ctrl)[0]

    def exit(self) -> None:
        try:
            _send(self.ctrl, {"cmd": "exit"})
        except OSError:
            pass
        self.ctrl.close()
