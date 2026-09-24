def run(items):
    out = []
    for it in items:
        out.append(step(it))
    return out

def step(v):
    return v + 1
