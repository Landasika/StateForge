"""DSVW 的 Atheris 靶函数(进程内回环 HTTP,批 1)。

bytes → {method, path, body, headers} → http.client 回环(harness 内
ThreadingServer)。dsvw 无 test_client 形态(BaseHTTPRequestHandler),
走回环驱动——批 3 WSGI/HTTP 回环适配层同族。
"""

from stateforge.targets.apps20.dsvw.harness import (  # noqa: F401
    _CONN as CONN,
)


class LoopbackAdapter:
    """JSON bytes → 回环 HTTP(dsvw 族:非框架 WSGI/HTTP 服务)。"""

    def execute(self, data: bytes) -> object:
        import json

        try:
            seed = json.loads(data) if data else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            seed = {}
        if not isinstance(seed, dict):
            seed = {}
        method = str(seed.get("method", "GET")).upper()
        path = str(seed.get("path", "/"))
        try:
            CONN.request(method, path)
            resp = CONN.getresponse()
            resp.read()
            return resp
        except Exception:                       # noqa: BLE001  fuzz 输入不 crash
            return None


_ADAPTER = LoopbackAdapter()


def target(data: bytes) -> None:
    _ADAPTER.execute(data)


if __name__ == "__main__":
    import atheris

    atheris.Setup([], target)
    atheris.Fuzz()
