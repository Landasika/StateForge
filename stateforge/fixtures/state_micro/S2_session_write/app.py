def login(session, uid):
    session["user_id"] = uid
    return "ok"
