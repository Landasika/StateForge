"""Target adapters: fuzz bytes -> one target execution."""
import pytest
from flask import Flask

from stateforge.adapters.base import TargetAdapter
from stateforge.adapters.callable import CallableAdapter
from stateforge.adapters.flask import FlaskAdapter


class FakeAtheris:
    def __init__(self):
        self.calls = []

    def instrument_func(self, func):
        self.calls.append(func)


def _app() -> Flask:
    app = Flask(__name__)

    @app.get("/items/<int:n>")
    def item(n: int):
        return {"n": n}

    @app.post("/orders")
    def create():
        from flask import request
        body = request.get_json(silent=True) or {}
        return {"total": body.get("total", 0)}, 201

    @app.get("/raise")
    def raiser():
        raise RuntimeError("view error")

    app.config["TESTING"] = True    # 异常传播:断言 adapter 不吞异常
    return app


def test_protocol_conformance():
    adapter = FlaskAdapter(_app())
    assert isinstance(adapter, TargetAdapter)


def test_routes_enumerated_path_params_prefilled():
    adapter = FlaskAdapter(_app())
    urls = {r.endpoint: r.url for r in adapter.routes}
    assert urls["item"] == "/items/1"          # 路径参数预填
    create = next(r for r in adapter.routes if r.endpoint == "create")
    assert create.method == "POST"             # 非 GET 路由取真实方法
    assert len(adapter.routes) == 3


def test_execute_dispatches_full_request_with_body():
    adapter = FlaskAdapter(_app(), bodies={"create": lambda d: {"total": d[1]}})
    resp = adapter.execute(b"\x01\x07")        # 第 2 条路由(create)
    assert resp is not None and resp.status_code == 201
    assert b'"total":7' in resp.data            # bytes 参与了 body 构造


def test_execute_swallows_nothing_view_exception_propagates():
    adapter = FlaskAdapter(_app())
    with pytest.raises(RuntimeError):
        adapter.execute(b"\x02")               # raiser —— 异常边界在 runner


def test_callable_adapter_prepare_instruments():
    atheris = FakeAtheris()
    calls = []

    def target(data: bytes):
        calls.append(data)

    adapter = CallableAdapter(target)
    adapter.prepare(atheris)
    adapter.execute(b"abc")
    assert calls == [b"abc"]
    assert atheris.calls == [target]
