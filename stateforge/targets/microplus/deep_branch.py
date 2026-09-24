"""Level-A 靶(SF-Micro+):三层魔法字节门 + 一条旁路,验证结构反馈闭环。"""


def target(data: bytes) -> None:
    a = data[0] if len(data) > 0 else 0
    b = data[1] if len(data) > 1 else 0
    c = data[2] if len(data) > 2 else 0
    if a == 0x42:
        stage1()
        if b == 0x17:
            stage2()
            if c == 0x99:
                deep()
    elif a == 0x07:
        alt_path(data)


def stage1():
    return 1


def stage2():
    return 2


def deep():
    return "hit"


def alt_path(data: bytes):
    total = 0
    for byte in data:
        total += byte
    return total
