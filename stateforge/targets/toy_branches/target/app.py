"""定向灰盒玩具靶:一个真分支 + 三个干扰分支,考"选对分支"而非"走得更深"。

``choice==0`` 才是通往目标的那条;其余三条都是死路,距离上可能同样近,
故 campaign 必须靠"分支是否朝目标方向"而不是单纯"执行得更深"来收敛。
"""

from flask import Flask, jsonify, request

app = Flask(__name__)


def deep_target(choice: int, a: int, b: int):
    if choice == 0:
        if a > 20:
            if b > 100:
                return "TARGET_REACHED"
        return "NOT_REACHED"
    if choice == 1:
        return "NOT_REACHED"
    if choice == 2:
        return "NOT_REACHED"
    return "NOT_REACHED"


@app.post("/check")
def check():
    data = request.get_json(silent=True) or {}
    values = [data.get(name, 0) for name in ("choice", "a", "b")]
    values = [value if isinstance(value, int) else 0 for value in values]
    return jsonify({"result": deep_target(*values)})
