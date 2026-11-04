"""toy_branches 适配器:前四字节 → POST /check 的 choice/a/b。"""

from stateforge.targets.toy_branches.target.app import app


class ToyAdapter:
    def __init__(self):
        self.client = app.test_client()

    def execute(self, data: bytes):
        values = [data[index] if len(data) > index else 0
                  for index in range(3)]
        return self.client.post(
            "/check",
            json={"choice": values[0], "a": values[1], "b": values[2]},
        )


def build_adapter():
    return ToyAdapter()
