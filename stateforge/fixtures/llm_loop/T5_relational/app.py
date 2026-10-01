def run(request):
    x = int(request.args["x"])
    y = int(request.args["y"])
    if x + y == 1337:
        return hidden()
    return 0

def hidden():
    return "found"
