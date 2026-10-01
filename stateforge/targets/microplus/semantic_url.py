def target(data: bytes) -> None:
    path = data.decode("utf-8", "replace").strip()
    if path.startswith("/api/v"):
        if path == "/api/v1/admin/config":
            hidden_config()

def hidden_config():
    return "found"
