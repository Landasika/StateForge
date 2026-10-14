"""Trac 靶 harness(项目跟踪系统,WSGI + sqlite 原生,批 3)。

形态:trac-admin initenv 建环境(每进程临时目录,内嵌 sqlite)
→ trac.web.main.dispatch_request(WSGI callable)→ wsgi_adapter 直驱。
匿名浏览 + 组件注入伪登录授权层。

状态语义(NOTES.md 详):
- DB 域(sqlite 原生):ticket 工作流(new→assigned→closed)、
  milestone/优先级、wiki 页版本史;
- 权限域:TRAC_ADMIN / TICKET_MODIFY 门(匿名只读)。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

VENV_BIN = Path(__file__).resolve().parents[4] / "external" / "trac" / ".venv" / "bin"

_ENVDIR = Path(tempfile.mkdtemp(prefix=f"trac_env_{os.getpid()}_"))
subprocess.run(
    [str(VENV_BIN / "trac-admin"), str(_ENVDIR), "initenv",
     "probe", "sqlite:db/trac.db"],
    input="\n" * 8, capture_output=True, text=True, timeout=600, check=True)
# 种子数据(Python API):2 张 ticket + 1 个 wiki 页(真实 DB 状态)
from trac.env import Environment as TracEnvironment  # noqa: E402
from trac.ticket import Ticket  # noqa: E402
from trac.wiki import WikiPage  # noqa: E402

_TENV = TracEnvironment(str(_ENVDIR))
with _TENV.db_transaction:
    for summary in ("probe ticket one", "probe ticket two"):
        t = Ticket(_TENV)
        t["summary"] = summary
        t["reporter"] = "probe"
        t.insert()
    page = WikiPage(_TENV, "ProbePage")
    page.text = "probe wiki page body"
    page.save("probe", "seed")

from trac.web.main import dispatch_request  # noqa: E402
from trac.test import EnvironmentStub  # noqa: F401  (依赖预热,可选)

from stateforge.adapters.wsgi import wsgi_adapter  # noqa: E402


class _AuthedDispatch:
    """dispatch_request + TRAC_ENV 注入(每请求隔离 environ 键)。"""

    def __call__(self, environ, start_response):
        environ["trac.env_path"] = str(_ENVDIR)
        environ["trac.base_url"] = "http://testserver/"
        return dispatch_request(environ, start_response)


APP = _AuthedDispatch()
ADAPTER = wsgi_adapter(APP)

ROUTES: list[tuple[str, str]] = [
    ("GET", "/"),
    ("GET", "/wiki"),
    ("GET", "/wiki/WikiStart"),
    ("GET", "/wiki/TracGuide"),
    ("GET", "/wiki/ProbePage"),
    ("GET", "/wiki/TracSearch"),
    ("GET", "/wiki/TracTickets"),
    ("GET", "/timeline"),
    ("GET", "/roadmap"),
    ("GET", "/report"),
    ("GET", "/report/1"),
    ("GET", "/report/7"),
    ("GET", "/report/8"),
    ("GET", "/search"),
    ("GET", "/search?q=probe"),
    ("GET", "/search?q=ticket"),
    ("GET", "/ticket/1"),
    ("GET", "/ticket/2"),
    ("GET", "/query"),
    ("GET", "/query?status=new"),
    ("GET", "/query?desc=1&sort=id"),
    ("GET", "/milestone"),
    ("GET", "/milestone/milestone1"),
    ("GET", "/prefs"),
    ("GET", "/about"),
]


def smoke() -> list[tuple[str, int]]:
    from werkzeug.test import create_environ, run_wsgi_app

    out = []
    for method, path in ROUTES:
        try:
            env = create_environ(method=method, path=path,
                                 base_url="http://testserver")
            app_iter, status, _h = run_wsgi_app(APP, env)
            try:
                list(app_iter)
            except Exception:                   # noqa: BLE001  惰性响应体
                pass
            out.append((f"{method} {path}", int(str(status).split()[0])))
        except Exception as exc:                # noqa: BLE001
            out.append((f"{method} {path}", f"EXC:{type(exc).__name__}:{exc}"))
    return out


if __name__ == "__main__":
    results = smoke()
    ok = sum(1 for _, s in results if isinstance(s, int) and 100 <= s < 500)
    print(f"smoke: {ok}/{len(results)} non-5xx env={_ENVDIR}")
    for route, status in results:
        print(f"  {status!s:>4}  {route}")
