def admin_delete(session, db, request):
    if not session.get("user_id"):
        return 401

    user = db.query(User).get(session["user_id"])

    if user.role != "admin":
        return 403

    if request.args.get("confirm") == "yes":
        return dangerous()

    return "not confirmed"

def dangerous():
    return "deleted"
