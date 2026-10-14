"""BenchmarkPython 的 Atheris 靶函数(GenericWebAdapter,Flask,批 1)。

运行解释器 = external/BenchmarkPython/.venv/bin/python;cwd 须为仓库根。
"""

from stateforge.targets.apps20.benchmarkpython.harness import CLIENT  # noqa: F401


def _make_adapter():
    from stateforge.adapters.generic_web import GenericWebAdapter

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
