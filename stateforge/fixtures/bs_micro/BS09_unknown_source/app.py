def compute():
    return 3


def run(request):
    x = compute()
    if x == 731:
        return prize()
    return 0

def prize():
    return "hit"
