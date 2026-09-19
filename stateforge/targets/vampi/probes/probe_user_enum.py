"""用户/口令枚举探针:三连响应差分。

存在+对口令 / 存在+错口令 / 不存在 —— vuln=1 消息可区分(枚举);vuln=0 同一话术。
用法:python probe_user_enum.py(需 VAMPI_BASE;先 GET /createdb)
"""

from _probe_lib import call, jbody, run, uniq, verdict


def main() -> int:
    call("GET", "/createdb")
    user = uniq("enum")
    call("POST", "/users/v1/register",
         {"username": user, "password": "right-pw", "email": f"{user}@x.io"})
    msgs = {}
    for label, body in [
        ("ok", {"username": user, "password": "right-pw"}),
        ("wrong-pw", {"username": user, "password": "bad-pw"}),
        ("no-user", {"username": uniq("ghost"), "password": "whatever"}),
    ]:
        _, resp = call("POST", "/users/v1/login", body)
        msgs[label] = jbody(resp).get("message", resp)
    distinguishable = len({msgs["ok"], msgs["wrong-pw"], msgs["no-user"]}) >= 2 and (
        msgs["wrong-pw"] != msgs["no-user"]
    )
    return verdict("user/password enumeration", distinguishable)


if __name__ == "__main__":
    run(main)
