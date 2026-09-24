def run(x):
    try:
        v = int(x)
        return v
    except ValueError:
        return -1
