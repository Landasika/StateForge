def run(request):
    x = int(request.args["x"])
    if x > 100:
        return prize()
    return 0

def prize():
    return "hit"
