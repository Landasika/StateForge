"""通用 Web 适配器:JSON 字节 → HTTP 请求 → 任意 Flask/Django test_client。

Atheris 给 bytes → json.loads → {method, path, body, headers} → client.request。
每个应用只需提供 app 对象 + 可选认证头,无需定制路由/body 映射。
"""

from __future__ import annotations

import json
from typing import Any, Protocol


class _TestClient(Protocol):
    def get(self, path, **kw): ...
    def post(self, path, **kw): ...
    def request(self, method, path, **kw): ...


class GenericWebAdapter:
    """适配任意有 test_client() 的 WSGI 应用(Flask/Django)。"""

    def __init__(self, app: Any, auth_headers: dict | None = None,
                 base_url: str = ""):
        self.client: _TestClient = app.test_client()
        self.auth_headers = auth_headers or {}
        self.base_url = base_url

    def prepare(self, atheris) -> None:
        from stateforge.fuzz.instrumentation import Instrumenter
        instrumenter = Instrumenter()
        # 选择性插桩:视图函数
        for name in dir(self.client):
            obj = getattr(self.client, name, None)
            if callable(obj) and hasattr(obj, "__code__"):
                instrumenter.instrument_callable(obj, atheris)

    def execute(self, data: bytes) -> object:
        """bytes → JSON → HTTP 请求(自动适配 Flask/Django test client)。"""
        try:
            seed = json.loads(data) if data else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            seed = {}
        if not isinstance(seed, dict):
            seed = {}

        method = str(seed.get("method", "GET")).upper()
        path = self.base_url + str(seed.get("path", "/"))
        body = seed.get("body")
        headers = {**self.auth_headers, **(seed.get("headers") or {})}

        try:
            return self._request(method, path, body, headers)
        except Exception:                    # noqa: BLE001  fuzz 输入不 crash
            return None

    def _request(self, method, path, body, headers):
        # Flask test client 有 .open();Django 没有(即使 Django 5.x 支持 headers=)
        is_flask = hasattr(self.client, "open")
        if is_flask:
            if method == "GET":
                return self.client.get(path, headers=headers)
            if body is not None:
                return self.client.request(method, path, json=body,
                                            headers=headers)
            return self.client.request(method, path, headers=headers)
        else:
            # Django: HTTP 头用 HTTP_ 前缀关键字参数
            extra = {f"HTTP_{k.replace('-', '_').upper()}": v
                     for k, v in headers.items()}
            if method == "GET":
                return self.client.get(path, **extra)
            data = json.dumps(body) if body is not None else None
            ct = "application/json" if body is not None else None
            return self.client.generic(method, path, data=data,
                                        content_type=ct, **extra)


def _client_sig(client) -> set:
    """检查 client.get 是否接受 headers 参数(区分 Flask vs Django)。"""
    import inspect
    try:
        return set(inspect.signature(client.get).parameters.keys())
    except (ValueError, TypeError):
        return set()
