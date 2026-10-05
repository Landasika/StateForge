def handler(session):
    if session.get("user_id"):
        return dashboard()
    return redirect("/login")

def dashboard():
    return "dashboard"

def redirect(path):
    return f"redirect:{path}"
