"""toy_app 适配器:输入前两字节 → POST /check 的 x/y。"""

from stateforge.targets.toy_app.target.app import app


class ToyAdapter:
    def __init__(self):
        self.client = app.test_client()

    def execute(self, data: bytes):
        x = data[0] if len(data) > 0 else 0
        y = data[1] if len(data) > 1 else 0
        return self.client.post("/check", json={"x": x, "y": y})


def build_adapter():
    return ToyAdapter()
