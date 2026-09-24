def run(items):
    hits = 0
    for it in items:
        if it < 0:
            continue
        if it == 99:
            break
        hits = hits + 1
    return hits
