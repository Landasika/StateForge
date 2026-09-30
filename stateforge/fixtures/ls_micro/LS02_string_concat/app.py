def run(request):
    name = request.args["name"]
    key = "user_" + name
    if key == "user_admin":
        return prize()
    return 0

def prize():
    return "hit"
