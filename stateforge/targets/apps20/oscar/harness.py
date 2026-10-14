"""django-oscar sandbox 靶 harness(Django 电商,批 2)。

sandbox = oscar 官方演示店(catalogue/basket/checkout/accounts)。
启动:DATABASE_NAME env → 每进程 sqlite → migrate → superuser → 会话。

状态语义(NOTES.md 详):
- 会话域:账户登录门(checkout/address 深块);
- DB 域:**订单/支付流**(basket→checkout→order 状态机)——与
  create→pay→ship 主张同构的旗舰靶;
- 复合:库存/合作伙伴门。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4] / "external" / "django-oscar" / "sandbox"

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sandbox.settings")
sys.path.insert(0, str(REPO.parent))
sys.path.insert(0, str(REPO))            # ROOT_URLCONF='urls' 为 sandbox 顶层模块
os.chdir(REPO)

_DB = Path(os.environ.get("SF_DB", REPO / f".sf_db_{os.getpid()}.sqlite"))
os.environ["DATABASE_NAME"] = str(_DB)     # sandbox settings import 期读取

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

settings.DATABASES["default"]["NAME"] = str(_DB)
settings.ALLOWED_HOSTS = ["*"]

from django.core.management import call_command  # noqa: E402

try:
    call_command("migrate", verbosity=0, interactive=False, run_syncdb=True)
except Exception:
    # 已知:catalogue.0017 在 Django6/py3.13 环境确定性触发 sqlite
    # "unable to open database file"(仅 DDL 字段属性变更)→ fake 后续跑
    import django.db

    try:
        call_command("migrate", "catalogue", "0017_auto_20190816_0938",
                     verbosity=0, interactive=False, fake=True)
        call_command("migrate", verbosity=0, interactive=False, run_syncdb=True)
    except django.db.Error as exc:               # noqa: BLE001
        print(f"[oscar] migrate fallback note: {exc}", file=sys.stderr)

_USER, _PASS = "probe", "Passw0rd!"
from django.contrib.auth import get_user_model  # noqa: E402

User = get_user_model()
if not User.objects.filter(username=_USER).exists():
    User.objects.create_superuser(_USER, "probe@oscar.local", _PASS)

from django.test import Client  # noqa: E402

CLIENT = Client()
CLIENT.login(username=_USER, password=_PASS)

ROUTES: list[tuple[str, str]] = [
    ("GET", "/"),
    ("GET", "/catalogue/"),
    ("GET", "/search/"),
    ("GET", "/basket/"),
    ("GET", "/accounts/"),
    ("GET", "/accounts/login/"),
    ("GET", "/accounts/register/"),
    ("GET", "/accounts/password-reset/"),
    ("GET", "/checkout/"),
    ("GET", "/shipping-address/"),
    ("GET", "/payment/"),
    ("GET", "/order-placement/"),
    ("GET", "/orders/"),
    ("GET", "/offers/"),
    ("GET", "/coupons/"),
    ("GET", "/dashboard/"),
    ("GET", "/dashboard/catalogue/"),
    ("GET", "/dashboard/orders/"),
    ("GET", "/dashboard/users/"),
    ("GET", "/dashboard/partners/"),
    ("GET", "/dashboard/reports/"),
    ("GET", "/dashboard/comms/"),
    ("GET", "/dashboard/reviews/"),
    ("GET", "/admin/"),
]


def smoke() -> list[tuple[str, int]]:
    out = []
    for method, path in ROUTES:
        try:
            r = CLIENT.get(path) if method == "GET" else CLIENT.post(path)
            out.append((f"{method} {path}", r.status_code))
        except Exception as exc:                 # noqa: BLE001
            out.append((f"{method} {path}",
                        f"EXC:{type(exc).__name__}:{exc}"))
    return out


if __name__ == "__main__":
    results = smoke()
    ok = sum(1 for _, s in results if isinstance(s, int) and s < 500)
    print(f"smoke: {ok}/{len(results)} non-5xx")
    for route, status in results:
        print(f"  {status!s:>4}  {route}")
