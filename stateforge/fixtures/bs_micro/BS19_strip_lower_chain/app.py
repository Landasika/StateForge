def run(request):
    n = request.args["name"].strip().lower()
    if n == "admin":
        return prize()
    return 0

def prize():
    return "hit"
