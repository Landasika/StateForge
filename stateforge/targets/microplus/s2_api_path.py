import json

def target(data: bytes) -> None:
    try:
        seed = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        seed = {"query": {}}
    q = seed.get("query", seed)
    path = str(q.get("path", ""))
    method = str(q.get("method", ""))
    version = str(q.get("version", ""))

    if not path.startswith("/"):
        return
    if version == "v1":
        _route_v1(path, method)
    elif version == "v2":
        _route_v2(path, method)

def _route_v1(path, method):
    if path == "/api/v1/admin/config":
        if method == "GET":
            _admin_config()
    elif path == "/api/v1/users/list":
        if method == "GET":
            _users_list()

def _route_v2(path, method):
    if path == "/api/v2/admin/settings":
        if method == "PUT":
            _admin_settings()
    elif path == "/api/v2/debug/dump":
        if method == "POST":
            _debug_dump()

def _admin_config():
    pass

def _users_list():
    pass

def _admin_settings():
    pass

def _debug_dump():
    pass
