def rot13c(ch):
    base = ord('a') if ch.islower() else ord('A')
    return chr((ord(ch) - base + 13) % 26 + base) if ch.isalpha() else ch


def run(request):
    raw = request.args["secret"]
    scrambled = "".join(rot13c(c) for c in raw)
    if scrambled == "nqzva":
        return prize()
    return 0

def prize():
    return "hit"
