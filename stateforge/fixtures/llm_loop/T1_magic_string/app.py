def run(request):
    s = request.args["payload"]
    if s.startswith("MAGIC"):
        if "STATEFORGE" in s:
            return hidden()
    return 0

def hidden():
    return "found"
