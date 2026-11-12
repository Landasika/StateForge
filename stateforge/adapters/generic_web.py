"""JSON request seeds transported through explicit Werkzeug or Django clients."""

from __future__ import annotations

import json
from typing import Any, Callable


Transport = Callable[[Any, str, str, Any, dict], object]


def werkzeug_request(client, method: str, path: str, body, headers: dict):
    """Flask and generic WSGI clients both expose Werkzeug's open API."""
    options = {"headers": headers}
    if method != "GET" and body is not None:
        options["json"] = body
    return client.open(path, method=method, **options)


def django_request(client, method: str, path: str, body, headers: dict):
    """Use Django's generic API without replacing methods on the shared client."""
    extra = {f"HTTP_{k.replace('-', '_').upper()}": v for k, v in headers.items()}
    if method == "GET":
        return client.get(path, **extra)
    return client.generic(method, path,
                          data=json.dumps(body) if body is not None else b"",
                          content_type="application/json", **extra)


class GenericWebAdapter:
    """Accept an application or an existing, persistent test client.

    Transport can be supplied explicitly for other frameworks. By default the
    adapter retains the historical fuzz boundary: target errors yield None.
    Set suppress_errors=False when the caller owns exception reporting.
    """

    def __init__(self, app: Any = None, auth_headers: dict | None = None,
                 base_url: str = "", *, client: Any = None,
                 transport: Transport | None = None, suppress_errors: bool = True):
        if client is None:
            if app is None:
                raise ValueError("app or client is required")
            client = app.test_client()
        self.client = client
        self.auth_headers = dict(auth_headers or {})
        self.base_url = base_url
        self.transport = transport or (werkzeug_request if hasattr(client, "open")
                                       else django_request)
        self.suppress_errors = suppress_errors

    @property
    def codec(self):
        """The input format this adapter consumes.

        ``execute`` parses a JSON request envelope, so a structure-blind
        mutator destroys the envelope and the adapter silently falls back to
        ``GET /``. Exposing the codec lets the campaign mutate the parsed
        request instead of its serialization, and keeps that knowledge here
        rather than in the generic mutator.
        """
        from stateforge.fuzzing.guidance.mutator import JsonCodec

        return JsonCodec()

    def prepare(self, atheris) -> None:
        from stateforge.fuzzing.instrumentation import Instrumenter
        instrumenter = Instrumenter()
        for name in dir(self.client):
            obj = getattr(self.client, name, None)
            if callable(obj) and hasattr(obj, "__code__"):
                instrumenter.instrument_callable(obj, atheris)

    def execute(self, data: bytes) -> object:
        try:
            seed = json.loads(data) if data else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            seed = {}
        if not isinstance(seed, dict):
            seed = {}
        method = str(seed.get("method", "GET")).upper()
        path = self.base_url + str(seed.get("path", "/"))
        supplied = seed.get("headers")
        headers = {**self.auth_headers, **(supplied if isinstance(supplied, dict) else {})}
        try:
            return self._request(method, path, seed.get("body"), headers)
        except Exception:  # compatibility boundary for arbitrary fuzz inputs
            if not self.suppress_errors:
                raise
            return None

    def _request(self, method, path, body, headers):
        return self.transport(self.client, method, path, body, headers)
