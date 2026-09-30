def run(request):
    tok = request.cookies["tok"]
    if tok == "s3cret":
        return prize()
    return 0

def prize():
    return "hit"
