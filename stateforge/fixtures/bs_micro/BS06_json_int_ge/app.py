def run(request):
    c = request.json["count"]
    if c >= 10:
        return prize()
    return 0

def prize():
    return "hit"
