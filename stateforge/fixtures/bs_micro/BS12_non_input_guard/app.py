def run(request):
    if request.session["authenticated"]:
        return prize()
    return 0

def prize():
    return "hit"
