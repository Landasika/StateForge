def run(request):
    x = int(request.args["x"])
    if 10 <= x:
        return prize()
    return 0

def prize():
    return "hit"
