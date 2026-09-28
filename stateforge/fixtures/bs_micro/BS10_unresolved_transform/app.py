def run(request):
    x = int(request.args["x"]) % 101
    if x == 13:
        return prize()
    return 0

def prize():
    return "hit"
