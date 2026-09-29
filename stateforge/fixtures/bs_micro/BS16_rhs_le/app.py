def run(request):
    x = int(request.args["x"])
    if 100 >= x:
        return prize()
    return 0

def prize():
    return "hit"
