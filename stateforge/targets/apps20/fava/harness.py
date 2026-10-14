"""fava 靶 harness(PyPI fava 1.30,Flask + beancount,批 1)。

形态:venv(external/fava/.venv)里的 fava 包 + 最小 beancount 账本(临时目录
含 pid,不污染仓库)→ fava.application.create_app([账本]) → flask
test_client。路由 100% 来自 url_map dump(见 NOTES.md),bfile slug 取自账本
option "title" 的 slugify,运行时用 LEDGERS.first_slug() 求出,不硬猜。

状态语义(NOTES.md 详):
- FILE 域:账本 .beancount 文件即状态(open/close/transaction 条目);
  editor/api/source 写回文件,changed() 按 mtime 触发整账本重载;
- URL 域:account/filter/time 全局过滤器走 query 参数,g.filtered 预过滤
  所有报告的 entries;
- 漏洞面:document/statement 文件路径、query 的 beanquery 语句、editor 源码。
  冒烟只取良性值(bad hash/filename → 404,不碰任意路径 sink)。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[4] / "external" / "fava"

# --- 账本文件:临时目录(含 pid),最小 open/transaction/close 集 ---
_TMPDIR = Path(tempfile.mkdtemp(prefix=f"fava-harness-{os.getpid()}-"))
LEDGER_FILE = _TMPDIR / "ledger.beancount"
LEDGER_FILE.write_text(
    'option "title" "Harness Ledger"\n'
    "2020-01-01 open Assets:Cash\n"
    "2020-01-01 open Assets:Bank:Checking\n"
    "2020-01-01 open Expenses:Food\n"
    "2020-01-01 open Income:Salary\n"
    '2020-01-15 * "Grocery Store" "weekly shopping"\n'
    "  Expenses:Food  45.20 USD\n"
    "  Assets:Cash\n"
    '2020-02-01 * "Employer" "salary"\n'
    "  Assets:Bank:Checking  2000.00 USD\n"
    "  Income:Salary\n"
    "2021-01-01 close Assets:Cash\n",
    encoding="utf-8",
)

from fava.application import create_app          # noqa: E402  venv 内 PyPI fava

APP = create_app([str(LEDGER_FILE)], load=True)
CLIENT = APP.test_client()

# bfile slug = slugify(option "title");运行时求,不硬编码
SLUG = APP.config["LEDGERS"].first_slug()

_ACCOUNT = "Assets:Cash"          # 账本里 open 的真实账户
_REF = f"/{SLUG}/journal/"        # /jump 需要 Referer(fava 从 referer 重建 url)

# (method, path[, headers]) —— 全部来自 url_map dump,参数用种子值填充
ROUTES: list[tuple[str, str] | tuple[str, str, dict[str, str]]] = [
    ("GET", "/"),                                   # index → 302 默认页
    ("GET", f"/{SLUG}/"),
    ("GET", f"/{SLUG}/income_statement/"),
    ("GET", f"/{SLUG}/balance_sheet/"),
    ("GET", f"/{SLUG}/trial_balance/"),
    ("GET", f"/{SLUG}/journal/"),
    ("GET", f"/{SLUG}/journal/?account={_ACCOUNT}"),   # URL 域过滤器
    ("GET", f"/{SLUG}/query/"),
    ("GET", f"/{SLUG}/query/?query_string=balances"),  # beanquery 种子语句
    ("GET", f"/{SLUG}/commodities/"),
    ("GET", f"/{SLUG}/documents/"),
    ("GET", f"/{SLUG}/events/"),
    ("GET", f"/{SLUG}/holdings/"),
    ("GET", f"/{SLUG}/holdings/by_currency/"),
    ("GET", f"/{SLUG}/editor/"),
    ("GET", f"/{SLUG}/errors/"),
    ("GET", f"/{SLUG}/statistics/"),
    ("GET", f"/{SLUG}/options/"),
    ("GET", f"/{SLUG}/import/"),
    ("GET", f"/{SLUG}/help/"),
    ("GET", f"/{SLUG}/account/{_ACCOUNT}/"),
    ("GET", f"/{SLUG}/account/Nope:Nope/"),        # 不存在账户(空报表)
    ("GET", f"/{SLUG}/context/?entry_hash=none"),  # 坏 hash → 404
    ("GET", f"/{SLUG}/document/?filename=nope"),   # 非账本文档 → 404
    ("GET", f"/{SLUG}/download-journal/"),
    ("GET", "/jump?time=year", {"Referer": _REF}),         # 时间过滤器跳转
    ("GET", "/jump?account=" + _ACCOUNT, {"Referer": _REF + "?time=year"}),
    ("GET", f"/{SLUG}/api/changed"),
    ("GET", f"/{SLUG}/api/errors"),
    ("GET", "/nonexistent"),                       # 404 路径
]


def smoke() -> list[tuple[str, int | str]]:
    out = []
    for route in ROUTES:
        method, path = route[0], route[1]
        headers = route[2] if len(route) > 2 else {}
        try:
            r = CLIENT.open(path, method=method, headers=headers)
            out.append((f"{method} {path}", r.status_code))
        except Exception as exc:                   # noqa: BLE001
            out.append((f"{method} {path}",
                        f"EXC:{type(exc).__name__}:{exc}"))
    return out


if __name__ == "__main__":
    results = smoke()
    ok = sum(1 for _, s in results if isinstance(s, int) and s < 500)
    print(f"smoke: {ok}/{len(results)} non-5xx (slug={SLUG})")
    for route, status in results:
        print(f"  {status!s:>4}  {route}")
