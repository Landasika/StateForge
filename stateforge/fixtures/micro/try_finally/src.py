def run(x):
    out = 0
    try:
        out = x + 1
    finally:
        out = out * 2
    return out
