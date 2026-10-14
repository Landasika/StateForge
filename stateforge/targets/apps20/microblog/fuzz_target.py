"""microblog 的 Atheris 靶函数(GenericWebAdapter,Flask test_client,批 1)。"""

from stateforge.targets.apps20.microblog.harness import CLIENT, _BEARER  # noqa: F401


def _make_adapter():
    from stateforge.adapters.generic_web import GenericWebAdapter

    adapter = GenericWebAdapter.__new__(GenericWebAdapter)
    adapter.client = CLIENT           # Flask test_client(有 .open,自动走分支)
    adapter.auth_headers = _BEARER    # API 蓝图 Bearer;页面域靠 client 会话 cookie
    adapter.base_url = ""
    return adapter


_ADAPTER = _make_adapter()


def target(data: bytes) -> None:
    _ADAPTER.execute(data)


if __name__ == "__main__":
    import atheris

    atheris.Setup([], target)
    atheris.Fuzz()
