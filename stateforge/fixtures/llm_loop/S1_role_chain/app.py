def run(request):
    username = request.args["username"]
    action = request.args["action"]
    token = request.args["token"]

    if not validate_username(username):
        return error_invalid()

    user = load_user(username)
    if user is None:
        return error_not_found()

    role = get_role(user)
    if role == "administrator":
        if action == "delete":
            if token.startswith("adm_"):
                return privileged_delete(user)
        elif action == "read":
            return admin_read(user)
    elif role == "moderator":
        if action == "edit":
            return moderator_edit(user)

    return normal_view(user)


def validate_username(u):
    return len(u) >= 3 and u.isalnum()


def error_invalid():
    return "invalid"


def load_user(u):
    return {"name": u} if u in ("administrator", "moderator", "alice") else None


def error_not_found():
    return "not_found"


def get_role(u):
    return u["name"]


def privileged_delete(u):
    audit_log("delete", u)
    return "deleted"


def admin_read(u):
    return "admin_data"


def moderator_edit(u):
    return "edited"


def normal_view(u):
    return "view"


def audit_log(op, u):
    return f"{op}:{u['name']}"
