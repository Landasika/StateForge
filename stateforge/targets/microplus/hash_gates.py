"""Level-A 靶 v2(战役用):混合难度门。

- 2 个易门(字节相等,native cmp 反馈秒破)→ 曲线先爬升,机器可见
- 3 个算术哈希门(线性混合取模,无输入字节直接参与比较)→ native 无从
  反推,预期 B/C 双双平台化;这是 Phase-3 定向的实证对象
共 9 个函数,构图约 60+ 块。
"""


def target(data: bytes) -> None:
    a = data[0] if len(data) > 0 else 0
    b = data[1] if len(data) > 1 else 0
    c = data[2] if len(data) > 2 else 0
    d = data[3] if len(data) > 3 else 0
    if a == 0x11:
        easy_one()
    if b == 0x22:
        easy_two(d)
    if (a * 7 + b * 13) % 251 == 42:
        gate_one(c)
    if (b * 5 + c * 17 + d * 3) % 241 == 7:
        gate_two(a)
    if (a * 3 + b * 11 + c * 23 + d * 29) % 239 == 100:
        gate_three()


def easy_one():
    return 1


def easy_two(d: int):
    if d % 2 == 0:
        return even_path()
    return odd_path()


def even_path():
    return "even"


def odd_path():
    return "odd"


def gate_one(c: int):
    if c > 128:
        return deep_one_a()
    return deep_one_b()


def deep_one_a():
    return "g1a"


def deep_one_b():
    return "g1b"


def gate_two(a: int):
    return a + 1


def gate_three():
    return finale()


def finale():
    return "finale"
