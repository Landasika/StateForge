def decrypt(v):
    return v[::-1]


def run(request):
    x = int(decrypt(request.args["x"]))
    if x == 731:
        return prize()
    return 0

def prize():
    return "hit"
