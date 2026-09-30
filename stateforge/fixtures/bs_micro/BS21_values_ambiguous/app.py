def run(request):
    x = int(request.values["x"])
    if x == 731:
        return prize()
    return 0

def prize():
    return "hit"
