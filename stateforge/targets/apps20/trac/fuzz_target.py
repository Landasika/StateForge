"""Trac 的 Atheris 靶函数(WSGI 回环,批 3)。

bytes → {method, path, body, headers} → wsgi_adapter(dispatch_request)。
运行解释器 = external/trac/.venv/bin/python。
"""

from stateforge.adapters.wsgi import wsgi_adapter  # noqa: F401
from stateforge.targets.apps20.trac.harness import APP


_ADAPTER = wsgi_adapter(APP)


def target(data: bytes) -> None:
    _ADAPTER.execute(data)


if __name__ == "__main__":
    import atheris

    atheris.Setup([], target)
    atheris.Fuzz()
