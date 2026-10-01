def run(request):
    username = request.args["username"]
    action = request.args["action"]
    if username == "administrator":
        if action == "delete":
            return hidden()
    return 0

def hidden():
    return "found"
