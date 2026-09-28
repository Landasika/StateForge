def run(request):
    name = request.args["name"]
    if name == "admin":
        return prize()
    return 0

def prize():
    return "hit"
