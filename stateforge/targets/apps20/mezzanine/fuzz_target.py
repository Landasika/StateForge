"""Mezzanine 的 Atheris 靶函数(GenericWebAdapter,Django test_client,批 3)。

运行解释器 = external/mezzanine/.venv/bin/python。
"""

from stateforge.targets.apps20.mezzanine.harness import CLIENT  # noqa: F401


def _make_adapter():
    from stateforge.adapters.generic_web import GenericWebAdapter

    CLIENT.generic = lambda method, path, **kw: CLIENT.request(
        REQUEST_METHOD=method, PATH_INFO=path, **kw)
    adapter = GenericWebAdapter.__new__(GenericWebAdapter)
    adapter.client = CLIENT
    adapter.auth_headers = {}
    adapter.base_url = ""
    return adapter


_ADAPTER = _make_adapter()


def target(data: bytes) -> None:
    _ADAPTER.execute(data)


if __name__ == "__main__":
    import atheris

    atheris.Setup([], target)
    atheris.Fuzz()
