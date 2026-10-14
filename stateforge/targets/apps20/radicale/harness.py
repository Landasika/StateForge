"""Radicale 靶 harness(CalDAV/CardDAV 服务器,WSGI,批 3)。

形态:radicale.app.Application(WSGI callable)→ stateforge/adapters/wsgi.py
的 werkzeug test Client 直驱(批 3 WSGI 薄适配层首靶)。存储 = 临时
目录文件系统(每进程独立),认证 = htpasswd(plain 用户种子)。

状态语义(NOTES.md 详):
- 文件域:CalDAV 集合/日历/事件 = 文件树上的持久状态
  (MKCOL/MKCALENDAR 写 → PROPFIND/REPORT 读);
- 认证域:BasicAuth 门(401 未授权)。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from stateforge.adapters.wsgi import wsgi_adapter

_USER, _PASS = "probe", "passw0rd"

_STORAGE = Path(tempfile.mkdtemp(prefix=f"radicale_store_{os.getpid()}_"))
_HTPASSWD = _STORAGE.parent / f"radicale_htpasswd_{os.getpid()}"
# radicale auth_htpasswd 加密默认为 plain 时支持 "user:password" 明文行
_HTPASSWD.write_text(f"{_USER}:{_PASS}\n", encoding="utf-8")

from radicale import app as radicale_app  # noqa: E402
from radicale import config as radicale_config  # noqa: E402

_configuration = radicale_config.load()
_configuration.update({
    "storage": {"filesystem_folder": str(_STORAGE)},
    "auth": {"type": "htpasswd", "htpasswd_filename": str(_HTPASSWD),
             "htpasswd_encryption": "plain"},
    "rights": {"type": "owner_only"},
    "headers": {},
}, "harness")

APP = radicale_app.Application(_configuration)

_AUTH = ("Basic " + __import__("base64").b64encode(
    f"{_USER}:{_PASS}".encode()).decode())
ADAPTER = wsgi_adapter(APP, auth_headers={"Authorization": _AUTH})

_CAL = "probe-cal"
_CAL2 = "probe-cal-2"
_EVT = "probe-evt.ics"
_EVT_BODY = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//StateForge probe//EN\r\n"
    "BEGIN:VEVENT\r\n"
    f"UID:{_EVT}\r\n"
    "DTSTAMP:20260101T000000Z\r\n"
    "DTSTART:20260102T000000Z\r\n"
    "DTEND:20260103T000000Z\r\n"
    "SUMMARY:probe event\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)

ROUTES: list[tuple[str, str, str]] = [
    ("GET", "/.web", "public"),                    # web 指纹页
    ("GET", "/.web/index.html", "public"),
    ("GET", "/.web/favicon.png", "public"),
    ("GET", "/", "auth"),
    ("PROPFIND", "/", "auth"),                     # 根集合发现
    ("PROPFIND", f"/{_USER}/", "auth"),            # 用户家集合
    ("REPORT", f"/{_USER}/", "auth"),              # 家集合查询
    ("OPTIONS", "/", "auth"),                      # 能力协商
    ("MKCOL", f"/{_USER}/{_CAL}/", "auth"),        # 建日历集合(写状态)
    ("PROPFIND", f"/{_USER}/{_CAL}/", "auth"),
    ("REPORT", f"/{_USER}/{_CAL}/", "auth"),       # 事件查询
    ("PUT", f"/{_USER}/{_CAL}/{_EVT}", "auth"),    # 写事件
    ("GET", f"/{_USER}/{_CAL}/{_EVT}", "auth"),    # 读事件(写后可见)
    ("PROPFIND", f"/{_USER}/{_CAL2}/", "auth"),    # 第二集合(不存在)
    ("REPORT", f"/{_USER}/{_CAL2}/", "auth"),
    ("PUT", f"/{_USER}/{_CAL2}/{_EVT}", "auth"),
    ("GET", f"/{_USER}/{_CAL2}/{_EVT}", "auth"),
    ("DELETE", f"/{_USER}/{_CAL}/{_EVT}", "auth"),
    ("GET", "/nope/", "auth"),                     # 404 路径
    ("PROPFIND", "/nope/", "auth"),
    ("GET", f"/other-user/", "auth"),              # 越权路径(owner_only 403/404)
    ("PROPFIND", f"/other-user/{_CAL}/", "auth"),
]


def smoke() -> list[tuple[str, int]]:
    out = []
    for method, path, auth in ROUTES:
        try:
            headers = {"Authorization": _AUTH} if auth == "auth" else {}
            if method == "GET":
                resp = ADAPTER.client.get(path, headers=headers)
                code = resp.status_code
            else:
                from werkzeug.test import create_environ, run_wsgi_app

                env = create_environ(method=method, path=path,
                                     headers=list(headers.items()))
                if method in ("PUT",):
                    env["wsgi.input"] = __import__("io").BytesIO(
                        _EVT_BODY.encode())
                    env["CONTENT_LENGTH"] = str(len(_EVT_BODY))
                if method in ("PROPFIND", "REPORT"):
                    body = ('<?xml version="1.0"?>'
                            '<d:propfind xmlns:d="DAV:">'
                            '<d:prop><d:resourcetype/><d:getetag/></d:prop>'
                            '</d:propfind>')
                    env["wsgi.input"] = __import__("io").BytesIO(
                        body.encode())
                    env["CONTENT_LENGTH"] = str(len(body))
                app_iter, status, _hdrs = run_wsgi_app(APP, env)
                list(app_iter) if hasattr(app_iter, "__iter__") else None
                code = int(str(status).split()[0])
            out.append((f"{method} {path}", code))
        except Exception as exc:                  # noqa: BLE001
            out.append((f"{method} {path}", f"EXC:{type(exc).__name__}:{exc}"))
    return out


if __name__ == "__main__":
    results = smoke()
    ok = sum(1 for _, s in results if isinstance(s, int) and 100 <= s < 500
             and s != 401 or (isinstance(s, int) and 100 <= s < 500))
    print(f"smoke: {ok}/{len(results)} non-5xx storage={_STORAGE}")
    for route, status in results:
        print(f"  {status!s:>4}  {route}")
