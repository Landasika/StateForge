"""wsgi.py —— 任意 WSGI 应用 → GenericWebAdapter(批 3 薄适配层)。

werkzeug.test.Client 原生可驱动任何 WSGI callable(Radicale/Trac 等
非 Flask/Django 框架应用),无需起服务、无需 socket;配合
GenericWebAdapter 的 is_flask 分支(.open 存在)即接入既有 fuzz 管线。

用法:
    from stateforge.adapters.wsgi import wsgi_adapter
    adapter = wsgi_adapter(my_wsgi_app)
    adapter.execute(b'{"method":"GET","path":"/"}')
"""

from __future__ import annotations

from typing import Any, Callable

from stateforge.adapters.generic_web import GenericWebAdapter


def wsgi_adapter(app: Callable, auth_headers: dict | None = None,
                 base_url: str = "") -> GenericWebAdapter:
    """WSGI callable → GenericWebAdapter(werkzeug test Client 驱动)。

    app:PEP 3333 WSGI callable(environ, start_response)。
    依赖 werkzeug(test extra 自带);应用自身依赖由其 venv 提供。
    """
    from werkzeug.test import Client

    client: Any = Client(app)
    adapter = GenericWebAdapter.__new__(GenericWebAdapter)
    adapter.client = client          # werkzeug Client 有 .open → 走 Flask 分支
    adapter.auth_headers = auth_headers or {}
    adapter.base_url = base_url
    return adapter
