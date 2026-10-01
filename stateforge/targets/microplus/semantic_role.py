def target(data: bytes) -> None:
    username = data.decode("utf-8", "replace").strip().lower()
    if username == "administrator":
        if len(data) > 3 and data[3] == 0x64:  # 'd' for delete
            privileged_action()

def privileged_action():
    return "found"
