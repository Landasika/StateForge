def run(request):
    x = int(request.args["x"])
    y = int(request.args["y"])
    if x == 731:
        if y >= 18:
            return prize()
        return 1
    return 0

def prize():
    return "hit"
