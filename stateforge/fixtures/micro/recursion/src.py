def run(n):
    if n <= 1:
        return 1
    return n * run(n - 1)
