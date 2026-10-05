"""S9:User role lifecycle — register/promote/demote/admin_action."""


class User:
    def __init__(self, username):
        self.username = username
        self.role = "user"


USERS = {}


def register(username):
    USERS[username] = User(username)
    return USERS[username]


def promote(username):
    user = USERS.get(username)
    if user is None:
        return None
    user.role = "moderator"
    return user


def make_admin(username):
    user = USERS.get(username)
    if user is None:
        return None
    user.role = "admin"
    return user


def demote(username):
    user = USERS.get(username)
    if user is None:
        return None
    user.role = "user"
    return user


def admin_action(username):
    user = USERS.get(username)
    if user is None:
        return "not found"
    if user.role != "admin":
        return "forbidden"
    return "admin action executed"
