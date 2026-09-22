"""Selective instrumentation with dedup."""
from flask import Flask

from stateforge.fuzz.instrumentation import Instrumenter


class FakeAtheris:
    def __init__(self):
        self.calls: list = []

    def instrument_func(self, func):
        self.calls.append(func)   # 真实 atheris 会改写 __code__;假件只记录


def _app_with_closure() -> Flask:
    app = Flask(__name__)
    app.config["TESTING"] = True
    factor = 2

    @app.get("/double/<int:n>")
    def double(n: int):
        def scale(v):          # 闭包内部函数,必须被插桩
            return v * factor
        return {"r": scale(n)}

    @app.post("/boom")
    def boom():
        raise RuntimeError("boom")

    return app


def test_instruments_views_and_closure_cells_once():
    app = _app_with_closure()
    atheris = FakeAtheris()
    inst = Instrumenter(registry=set())
    first = inst.instrument_flask_app(app, atheris)
    calls_after_first = len(atheris.calls)
    second = inst.instrument_flask_app(app, atheris)
    assert first >= 2          # double + scale(闭包)+ boom
    assert second == 0         # 全部去重
    assert len(atheris.calls) == calls_after_first


def test_double_instrument_of_same_function_is_rejected():
    atheris = FakeAtheris()

    def f(x):
        return x + 1

    # 跨实例去重走默认模块级 registry(隔离 registry 只在单实例内去重)
    assert Instrumenter().instrument_callable(f, atheris) is True
    assert Instrumenter().instrument_callable(f, atheris) is False
    assert len(atheris.calls) == 1


def test_non_callable_and_instrument_failure_are_safe():
    atheris = FakeAtheris()

    class Exploding:
        def instrument_func(self, func):
            raise ValueError("not instrumentable")

    inst = Instrumenter(registry=set())
    assert inst.instrument_callable(None, atheris) is False
    assert inst.instrument_callable(42, atheris) is False

    def g():
        return 1

    assert inst.instrument_callable(g, Exploding()) is False   # 失败不上 registry
    assert inst.instrument_callable(g, atheris) is True        # 失败后可重试
