"""MoinMoin 靶 harness(wiki,Flask + 文件域存储,批 3 顶替 FlaskBB)。

形态:官方 `moin create-instance --full`(临时目录,含 pid)生成合法
wikiconfig + wiki 数据 + 索引 → create_app(wikiconfig.py)→ superuser
+ 会话登录。文件域状态的旗舰靶(页面树/版本史/用户档)。

状态语义(NOTES.md 详):
- 文件域:wiki 页面 = 文件系统上的持久树(改→版本+1;旧版可回看);
- 会话域:登录门(改页/受保护动作);
- ACL 域:页面级读/写权限(acl_rights)。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

VENV_BIN = (Path(__file__).resolve().parents[4] / "external" / "moin"
            / ".venv" / "bin")

_INST = Path(tempfile.mkdtemp(prefix=f"moin_inst_{os.getpid()}_"))
subprocess.run([str(VENV_BIN / "moin"), "create-instance", "--full"],
               cwd=_INST, capture_output=True, text=True, timeout=900,
               check=True)

from moin.app import create_app  # noqa: E402

APP = create_app(str(_INST / "wikiconfig.py"))

_USER, _PASS = "probe", "Passw0rd!"
with APP.app_context():
    from moin.user import User

    u = User(name=_USER, password=_PASS)
    u.email = "probe@moin.local"
    u.disable_mail = True
    u.save()

CLIENT = APP.test_client()
_r = CLIENT.post("/+login", data={
    "login_name": _USER, "login_password": _PASS, "submit": "Login"},
    follow_redirects=False)

ROUTES: list[tuple[str, str]] = [
    ("GET", "/"),                       # 首页
    ("GET", "/+index/"),
    ("GET", "/+index/all/"),
    ("GET", "/Home"),
    ("GET", "/HelpOnMoinWikiSyntax"),   # 内置帮助页(探查真实名)
    ("GET", "/RecentChanges"),
    ("GET", "/+search?q=probe"),
    ("GET", "/+search?q=Home"),
    ("GET", "/+history/Home"),
    ("GET", "/+users"),
    ("GET", "/+admin/user"),
    ("GET", "/+admin/interwikiname"),
    ("GET", "/+admin/trash"),
    ("GET", "/+login"),
    ("GET", "/+logout"),
    ("GET", "/+register"),
    ("GET", "/+lostpass"),
    ("GET", "/+mychanges"),
    ("GET", "/+refs/Home"),
    ("GET", "/+forwardrefs/Home"),
    ("GET", "/+backrefs/Home"),
    ("GET", "/+modify/Home"),           # 登录后编辑门(302/403/200)
    ("GET", "/NoSuchPageEver"),         # 404 路径
]


def smoke() -> list[tuple[str, int]]:
    out = []
    for method, path in ROUTES:
        try:
            r = CLIENT.get(path) if method == "GET" else CLIENT.post(path)
            out.append((f"{method} {path}", r.status_code))
        except Exception as exc:                 # noqa: BLE001
            out.append((f"{method} {path}", f"EXC:{type(exc).__name__}:{exc}"))
    return out


if __name__ == "__main__":
    results = smoke()
    ok = sum(1 for _, s in results if isinstance(s, int) and 100 <= s < 500)
    print(f"smoke: {ok}/{len(results)} non-5xx inst={_INST} login={_r.status_code}")
    for route, status in results:
        print(f"  {status!s:>4}  {route}")
