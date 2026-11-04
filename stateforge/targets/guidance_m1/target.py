"""定向灰盒最小靶:三道**阈值**门构成可验证的距离梯度。

``target`` 依次要求 ``data[0] > 20``、``data[1] > 100``、``data[2] > 150``
才到达 ``target_block``(跨两个被调函数)。选阈值比较而不是精确相等,是因为
只有"增量变异能逐步逼近"的条件才谈得上距离梯度——精确等值(魔数)是谓词
引导该解的问题,不是块级距离能解的问题,拿它当块级引导的靶会误判能力。
"""


def stage_one():
    return 1


def stage_two():
    return 2


def target_block():
    return 3


def target(data: bytes):
    if len(data) < 3:
        return None
    if data[0] > 20:
        stage_one()
        if data[1] > 100:
            stage_two()
            if data[2] > 150:
                return target_block()
    return None
