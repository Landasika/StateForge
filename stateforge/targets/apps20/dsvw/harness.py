"""DSVW 靶 harness(单文件 stdlib WSGI-外系,批 1)。

形态:http.server 线程服务 + :memory: sqlite → **进程内起线程服务 +
http.client 回环驱动**(批 3 WSGI/HTTP 回环适配层的原型)。

状态语义(NOTES.md 详):
- DB 域:comments 表(POST 写 → 空 comment 读,跨请求写/读对);
- 会话域:/login 按 users 表核验 → SESSIONID cookie 置/清;
- 漏洞面:参数驱动(id/v/name/comment/size/redir/tmpl/...)。
冒烟只取良性值(不触发 path/include/domain/object 危险 sinks)。
"""

from __future__ import annotations

import http.client
import importlib.util
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[2] / "dsvw"   # targets/dsvw/

_spec = importlib.util.spec_from_file_location("dsvw_app", REPO / "dsvw.py")
dsvw = importlib.util.module_from_spec(_spec)
sys.modules["dsvw_app"] = dsvw
_spec.loader.exec_module(dsvw)

dsvw.init()                                   # :memory: sqlite + users 种子

_server = dsvw.ThreadingServer(("127.0.0.1", 0), dsvw.ReqHandler)
_PORT = _server.server_address[1]
threading.Thread(target=_server.serve_forever, daemon=True).start()

_CONN = http.client.HTTPConnection("127.0.0.1", _PORT, timeout=10)

ROUTES: list[tuple[str, str]] = [
    ("GET", "/"),
    ("GET", "/?id=1"),
    ("GET", "/?id=2"),
    ("GET", "/?id=3"),
    ("GET", "/?v=probe"),
    ("GET", "/?name=admin"),
    ("GET", "/?name=nope"),
    ("GET", "/?comment="),                    # 读:comments 表列出
    ("GET", "/?comment=hello-from-harness"),  # 写:INSERT INTO comments
    ("GET", "/?size=2"),
    ("GET", "/?redir=/"),
    ("GET", "/?tmpl={0}"),
    ("GET", "/?charset=utf8"),
    ("GET", "/users.json"),
    ("GET", "/?callback=cb"),
    ("GET", "/users.json?callback=cb"),
    ("GET", "/login?username=admin&password=admin"),
    ("GET", "/login?username=admin&password=wrong"),
    ("GET", "/login?username=nope&password=x"),
    ("GET", "/nonexistent"),                  # 404 路径
    ("GET", "/?id=1&charset=utf8"),
    ("GET", "/?v=probe&name=admin"),
]


def _request(method: str, path: str) -> int:
    _CONN.request(method, path)
    r = _CONN.getresponse()
    r.read()
    return r.status


def smoke() -> list[tuple[str, int]]:
    out = []
    for method, path in ROUTES:
        try:
            out.append((f"{method} {path}", _request(method, path)))
        except Exception as exc:              # noqa: BLE001
            out.append((f"{method} {path}", f"EXC:{type(exc).__name__}"))
    return out


if __name__ == "__main__":
    results = smoke()
    ok = sum(1 for _, s in results if isinstance(s, int) and s < 500)
    print(f"smoke: {ok}/{len(results)} non-5xx (port={_PORT})")
    for route, status in results:
        print(f"  {status!s:>4}  {route}")
