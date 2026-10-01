def run(request):
    n = int(request.args["n"])
    if n >= 999:
        return hidden()
    return 0

def hidden():
    return "found"
