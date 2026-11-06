"""microblog 的 Atheris 靶函数(GenericWebAdapter,Flask test_client,批 1)。"""

from stateforge.targets.apps20.microblog.harness import CLIENT, _BEARER  # noqa: F401


def _make_adapter():
    from stateforge.adapters.generic_web import GenericWebAdapter

    return GenericWebAdapter(
        client=CLIENT,
        auth_headers=_BEARER,
        base_url="")


_ADAPTER = _make_adapter()


def target(data: bytes) -> None:
    _ADAPTER.execute(data)


if __name__ == "__main__":
    import atheris

    atheris.Setup([], target)
    atheris.Fuzz()
