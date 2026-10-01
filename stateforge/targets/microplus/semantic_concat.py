def target(data: bytes) -> None:
    s = data.decode("utf-8", "replace").strip()
    key = "user_" + s
    if key == "user_admin":
        deep()

def deep():
    return "found"
