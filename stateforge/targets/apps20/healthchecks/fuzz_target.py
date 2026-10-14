"""healthchecks 的 Atheris 靶函数(GenericWebAdapter 驱动,批 1)。

bytes → {method, path, body, headers} → django.test.Client(会话持久)。
运行解释器 = external/healthchecks/.venv/bin/python(应用依赖独立)。
启动引导与路由表见 harness.py。
"""

from stateforge.targets.apps20.healthchecks.harness import (  # noqa: F401
    API_KEY, CLIENT,
)


def _make_adapter():
    from stateforge.adapters.generic_web import GenericWebAdapter

    CLIENT.generic = lambda method, path, **kw: CLIENT.request(
        REQUEST_METHOD=method, PATH_INFO=path, **kw)
    adapter = GenericWebAdapter.__new__(GenericWebAdapter)
    adapter.client = CLIENT
    adapter.auth_headers = {"X-Api-Key": API_KEY} if API_KEY else {}
    adapter.base_url = ""
    return adapter


_ADAPTER = _make_adapter()


def target(data: bytes) -> None:
    _ADAPTER.execute(data)


if __name__ == "__main__":
    import atheris

    atheris.Setup([], target)
    atheris.Fuzz()
