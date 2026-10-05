def admin_panel(user):
    if user.role == "admin":
        return panel()
    return "forbidden"

def panel():
    return "admin"
