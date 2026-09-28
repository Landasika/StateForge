"""Phase-3.1 靶(顾问 §18.⑥):误导性高覆盖子树 vs 窄深路径。

高位=1(50%)进入 wide:8 个浅 mod 门,块多边多、随机即中——
Phase-2 novelty 的糖果罐,持续吸收预算。
高位=0 进入 narrow:4 级算术哈希门(线性混合取模,native 无从反推),
每级门后一层函数,终点 deep_prize。
Phase-3 期望:保留 narrow 路径上的近前沿冠军,而非 wide 的 novelty 流量。
"""

def target(data: bytes) -> None:
    a = data[0] if len(data) > 0 else 0
    if a & 0x80:
        wide(data)
    else:
        narrow(data)


def wide(data: bytes) -> None:
    b = data[1] if len(data) > 1 else 0
    c = data[2] if len(data) > 2 else 0
    if b % 2:
        w1()
    if b % 3:
        w2()
    if b % 5:
        w3()
    if b % 7:
        w4()
    if c % 2:
        w5()
    if c % 3:
        w6()
    if c % 5:
        w7()
    if c % 7:
        w8()


def w1(): return 1
def w2(): return 2
def w3(): return 3
def w4(): return 4
def w5(): return 5
def w6(): return 6
def w7(): return 7
def w8(): return 8


def narrow(data: bytes) -> None:
    b = data[1] if len(data) > 1 else 0
    if (b * 7) % 101 == 13:
        n1(data)


def n1(data: bytes) -> None:
    c = data[2] if len(data) > 2 else 0
    if (c * 5 + 1) % 97 == 7:
        n2(data)


def n2(data: bytes) -> None:
    d = data[3] if len(data) > 3 else 0
    if (d * 11 + 3) % 89 == 5:
        n3(data)


def n3(data: bytes) -> None:
    e = data[4] if len(data) > 4 else 0
    if (e * 13 + 7) % 83 == 11:
        deep_prize()


def deep_prize():
    return "prize"
