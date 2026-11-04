"""toy_nested 适配器:前四字节 → POST /check 的 a/b/c/d。"""

from stateforge.targets.toy_nested.target.app import app


class ToyAdapter:
    def __init__(self):
        self.client = app.test_client()

    def execute(self, data: bytes):
        values = [data[index] if len(data) > index else 0
                  for index in range(4)]
        return self.client.post(
            "/check",
            json={"a": values[0], "b": values[1],
                  "c": values[2], "d": values[3]},
        )


def build_adapter():
    return ToyAdapter()
