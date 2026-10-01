def run(request):
    raw = request.args["v"]
    cleaned = raw.strip()
    n = int(cleaned)
    if n == 4242:
        return hidden()
    return 0

def hidden():
    return "found"
