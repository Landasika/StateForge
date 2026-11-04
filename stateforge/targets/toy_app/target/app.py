"""定向灰盒玩具靶:两个嵌套数值门,越深越接近目标块。

``deep_target`` 的三条边构成可验证的距离阶梯(x>20 → y>100 → 命中),
是 campaign/调度/变异的最小闭环靶。
"""

from flask import Flask, jsonify, request

app = Flask(__name__)


def deep_target(x: int, y: int):
    if x > 20:
        if y > 100:
            return "TARGET_REACHED"
    return "NOT_REACHED"


@app.post("/check")
def check():
    data = request.get_json(silent=True) or {}
    x = data.get("x", 0)
    y = data.get("y", 0)
    if not isinstance(x, int):
        x = 0
    if not isinstance(y, int):
        y = 0
    return jsonify({"result": deep_target(x, y)})
