def run(a, b):
    if 0 < a < 100 and b:      # chained comparison + BoolOp → 不支持
        return prize()
    return 0

def prize():
    return "hit"
