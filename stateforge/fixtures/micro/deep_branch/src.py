def run(a, b, c):
    if a == 0x42:
        if b == 0x17:
            if c == 0x99:
                return target()
    return 0

def target():
    return "hit"
