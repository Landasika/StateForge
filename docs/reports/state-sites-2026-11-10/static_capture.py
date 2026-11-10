"""纯静态测量:sites.py 对 session 转移点的抓取率(零执行、零请求)。

基准(ground truth) = 应用代码里所有 session 链上的状态写:
  - 下标写/删/变更调用(x[k]=v, del, pop, setdefault, update, clear, flush)
  - 属性写(x.permanent = v —— 提取器当前不认,单列暴露形状缺口)
基准判定按"链最后一个成分是 session 且非 db.*"——这是给测量定的
基准,提取器本身仍不认识 "session" 这个词。

漏斗:
  raw(全部) → shared(≥2 函数共用) → qualified(+入口形参/导入资格)
  = 提取器实际产出
"""

from __future__ import annotations

import ast
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, "/home/landasika/icassp")
from stateforge.state.sites import (                   # noqa: E402
    extract_sites, make_entry_matcher)
from stateforge.analysis.analyzer import analyze          # noqa: E402

_MUT = {"pop", "setdefault", "update", "clear", "flush"}
_DBHINT = {"db", "database", "engine", "scoped", "sqlalchemy"}


def _chain(n):
    parts = []
    while isinstance(n, ast.Attribute):
        parts.append(n.attr); n = n.value
    if isinstance(n, ast.Name):
        parts.append(n.id)
        return ".".join(reversed(parts))
    return None


def _is_sess(chain: str | None) -> bool:
    if not chain:
        return False
    head, _, last = chain.rpartition(".")
    return last == "session" and not (_DBHINT & set(head.split(".")))


def raw_sites(root: Path, skip):
    """基准:session 链上的全部写形状(下标 + 变更调用 + 属性写)。"""
    sub = mut = attr = 0
    for p in root.rglob("*.py"):
        if skip(p) or "/.venv/" in str(p):   # 基准只算应用代码
            continue
        try:
            t = ast.parse(p.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for n in ast.walk(t):
            if isinstance(n, ast.Assign):
                for tg in n.targets:
                    if isinstance(tg, ast.Subscript) and _is_sess(_chain(tg.value)):
                        sub += 1
                    elif isinstance(tg, ast.Attribute) and _is_sess(_chain(tg.value)):
                        attr += 1
            elif isinstance(n, ast.Delete):
                for tg in n.targets:
                    if isinstance(tg, ast.Subscript) and _is_sess(_chain(tg.value)):
                        sub += 1
            elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr in _MUT and _is_sess(_chain(n.func.value)):
                mut += 1
    return sub, mut, attr


def froots_of(app):
    sp = next(Path(f"/home/landasika/icassp/external/{app}")
              .glob(".venv/lib/python3*/site-packages"))
    out = []
    for name in ("django/contrib", "flask", "flask_login", "werkzeug"):
        d = sp / name
        if d.is_dir():
            out.append(d.resolve())
    return out


def run(app, framework):
    root = Path(f"/home/landasika/icassp/external/{app}").resolve()
    fs = froots_of(app)

    def skip(p):
        if any(str(p).startswith(str(f) + "/") for f in fs):
            return False
        if {".venv", "site-packages", "__pycache__"} & set(p.parts):
            return True
        if {"test", "tests", "testing", "selftest"} & set(p.parts):
            return True
        return "test" in p.name.lower()

    model = analyze(root, framework)
    entries = {(Path(e.file).name, e.func) for e in model.endpoints}
    entry = make_entry_matcher(entries)

    raw_sub, raw_mut, raw_attr = raw_sites(root, skip)

    s_raw = extract_sites([root, *fs], skip=skip, min_functions=1)
    s_shared = extract_sites([root], skip=skip)                    # min_functions=2
    s_qual = extract_sites([root, *fs], skip=skip, is_entry=entry)

    def sess(scan, app_only=True):
        return [s for s in scan.stores
                if _is_sess(s.chain)
                and ("/.venv/" not in s.file
                     and str(root) in s.file)]

    n_raw = len(sess(s_raw))
    n_shared = len(sess(s_shared))
    n_qual = len(sess(s_qual))
    fw_qual = len([s for s in s_qual.stores
                   if _is_sess(s.chain) and "/.venv/" in s.file])

    keys = Counter(s.key_status for s in sess(s_qual))

    print(f"===== {app}")
    print(f"  基准: 下标写 {raw_sub} + 变更 {raw_mut} + 属性写 {raw_attr}"
          f" = {raw_sub + raw_mut + raw_attr}")
    print(f"  漏斗: raw {n_raw} → shared {n_shared} → qualified {n_qual}"
          f"  (框架内另捕获 {fw_qual})")
    base = raw_sub + raw_mut + raw_attr
    print(f"  抓取率(含属性写): {n_qual}/{base} = {n_qual / base:.0%}"
          if base else "  无 session 写")
    print(f"  属性写(形状缺口,全漏): {raw_attr}")
    print(f"  key 证据: {dict(keys)}")
    drops = n_raw - n_qual
    if drops:
        ex = [f"{Path(s.file).name}:{s.line} {s.chain}"
              for s in sess(s_raw) if s not in sess(s_qual)][:3]
        print(f"  漏掉示例: {ex}")
    return n_qual, base, raw_attr


if __name__ == "__main__":
    tot_q = tot_b = tot_a = 0
    for app, fw in [("healthchecks", "django"), ("CTFd", "flask"),
                    ("microblog", "flask"), ("linkding", "django")]:
        q, b, a = run(app, fw)
        tot_q += q; tot_b += b; tot_a += a
    print(f"\n合计: 下标抓取 {tot_q}/{tot_b} = {tot_q/tot_b:.0%};"
          f" 属性写缺口 {tot_a}")
