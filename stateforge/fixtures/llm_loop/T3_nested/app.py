def run(request):
    a = int(request.args["a"])
    b = int(request.args["b"])
    c = int(request.args["c"])
    if a == 111:
        if b == 222:
            if c == 333:
                return hidden()
    return 0

def hidden():
    return "found"
