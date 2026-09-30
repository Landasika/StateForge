def run(request):
    mode = request.headers["X-Mode"]
    if mode == "admin":
        return prize()
    return 0

def prize():
    return "hit"
