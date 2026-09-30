def run(request):
    v = float(request.args["v"])
    if v >= 1.5:
        return prize()
    return 0

def prize():
    return "hit"
