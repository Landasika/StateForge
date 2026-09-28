def run(request):
    x = int(request.args["x"])
    if x == 5:
        return 0
    return prize()

def prize():
    return "hit"
