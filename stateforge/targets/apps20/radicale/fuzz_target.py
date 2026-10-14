"""Radicale 的 Atheris 靶函数(WSGI 回环,批 3)。

bytes → {method, path, body, headers} → wsgi_adapter(werkzeug 直驱
CalDAV WSGI app)。运行解释器 = external/Radicale/.venv/bin/python。
"""

from stateforge.adapters.wsgi import wsgi_adapter  # noqa: F401
from stateforge.targets.apps20.radicale.harness import APP, _AUTH


def _make_adapter():
    return wsgi_adapter(APP, auth_headers={"Authorization": _AUTH})


_ADAPTER = _make_adapter()


def target(data: bytes) -> None:
    _ADAPTER.execute(data)


if __name__ == "__main__":
    import atheris

    atheris.Setup([], target)
    atheris.Fuzz()
