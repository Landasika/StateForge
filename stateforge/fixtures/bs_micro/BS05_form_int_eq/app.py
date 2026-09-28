def run(request):
    n = int(request.form["count"])
    if n == 42:
        return prize()
    return 0

def prize():
    return "hit"
