"""定向灰盒玩具靶:跨函数的两级门,考"距离能否穿过调用边"。

``deep_target`` 过两道门后调用 ``gate``,目标块在**被调函数**里——单函数
距离表会在这里失效,只有 ICFG 上的跨函数距离才指得对方向。
"""

from flask import Flask, jsonify, request

app = Flask(__name__)


def gate(c: int, d: int):
    if c > 150:
        if d > 200:
            return "TARGET_REACHED"
    return "NOT_REACHED"


def deep_target(a: int, b: int, c: int, d: int):
    if a > 20:
        if b > 100:
            return gate(c, d)
    return "NOT_REACHED"


@app.post("/check")
def check():
    data = request.get_json(silent=True) or {}
    values = [data.get(name, 0) for name in ("a", "b", "c", "d")]
    values = [value if isinstance(value, int) else 0 for value in values]
    return jsonify({"result": deep_target(*values)})
