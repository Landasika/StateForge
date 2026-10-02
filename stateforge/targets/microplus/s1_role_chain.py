import json

def target(data: bytes) -> None:
    try:
        seed = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        seed = {"query": {}}
    q = seed.get("query", seed)
    username = str(q.get("username", ""))
    action = str(q.get("action", ""))
    token = str(q.get("token", ""))

    if not _validate(username):
        return
    if username not in ("administrator", "moderator", "alice"):
        return
    if username == "administrator":
        if action == "delete":
            if token.startswith("adm_"):
                _privileged_delete()
        elif action == "read":
            _admin_read()
    elif username == "moderator":
        if action == "edit":
            _moderator_edit()
    _normal_view()

def _validate(u):
    return len(u) >= 3 and u.isalnum()

def _privileged_delete():
    _audit("delete")

def _admin_read():
    pass

def _moderator_edit():
    pass

def _normal_view():
    pass

def _audit(op):
    pass
