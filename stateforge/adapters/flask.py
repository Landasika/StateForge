"""Flask adapter: fuzz bytes -> one full_dispatch_request."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable, Mapping

from stateforge.fuzzing.instrumentation import Instrumenter

_PATH_PARAM = re.compile(r"<(?:int:|string:|path:|uuid:|float:)?\w+>")


@dataclass(frozen=True)
class RouteSpec:
    url: str
    endpoint: str
    method: str
    json_body: dict | None = None


class FlaskAdapter:
    """Enumerates routes, prefills path params, runs the full Flask stack.

    ``bodies`` maps endpoint -> dict or ``callable(data) -> dict``; fuzz bytes
    can drive request values through the callable form.
    """

    def __init__(self, app, headers: Mapping[str, str] | None = None,
                 bodies: Mapping[str, object] | None = None):
        self.app = app
        self.headers = dict(headers or {})
        self.bodies = dict(bodies or {})
        self.routes = self._enumerate()
        self._instrumenter = Instrumenter(registry=set())

    def _enumerate(self) -> list[RouteSpec]:
        routes = []
        for rule in self.app.url_map.iter_rules():
            if rule.endpoint == "static":
                continue
            methods = sorted(rule.methods - {"HEAD", "OPTIONS"})
            if not methods:
                continue
            method = "GET" if "GET" in methods else methods[0]
            routes.append(RouteSpec(_PATH_PARAM.sub("1", rule.rule),
                                    rule.endpoint, method))
        return routes

    def prepare(self, atheris) -> None:
        self._instrumenter.instrument_flask_app(self.app, atheris)

    def execute(self, data: bytes):
        if not self.routes or not data:
            return None
        route = self.routes[data[0] % len(self.routes)]
        body = self.bodies.get(route.endpoint)
        if callable(body):
            body = body(data)
        kwargs = {"method": route.method, "headers": self.headers}
        if body is not None:
            kwargs["json"] = body
        with self.app.test_request_context(route.url, **kwargs):
            return self.app.full_dispatch_request()
