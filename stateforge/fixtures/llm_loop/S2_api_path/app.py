def run(request):
    path = request.args["path"]
    method = request.args["method"]
    version = request.args["version"]

    if not path.startswith("/"):
        return error_bad_path()

    if version == "v1":
        handler = route_v1(path, method)
    elif version == "v2":
        handler = route_v2(path, method)
    else:
        return error_bad_version()

    return handler


def error_bad_path():
    return "bad_path"


def route_v1(path, method):
    if path == "/api/v1/admin/config":
        if method == "GET":
            return admin_config_v1()
    elif path == "/api/v1/users/list":
        if method == "GET":
            return users_list()
    return not_found_v1()


def route_v2(path, method):
    if path == "/api/v2/admin/settings":
        if method == "PUT":
            return admin_settings_v2()
    elif path == "/api/v2/debug/dump":
        if method == "POST":
            return debug_dump()
    return not_found_v2()


def error_bad_version():
    return "bad_version"


def admin_config_v1():
    return "v1_config"


def users_list():
    return "users"


def not_found_v1():
    return "nf_v1"


def admin_settings_v2():
    return "v2_settings"


def debug_dump():
    return "dump"


def not_found_v2():
    return "nf_v2"
