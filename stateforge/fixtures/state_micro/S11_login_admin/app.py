def login(session, uid):
    session["uid"] = uid
    return "ok"

def profile(session):
    uid = session.get("uid")
    if uid is None:
        return "401"
    return "profile"

def admin(session):
    role = session.get("role")
    if role == "admin":
        return "admin panel"
    return "forbidden"
