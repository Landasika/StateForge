"""跨全部应用跑整条静态链:端点 → 转移点抓取率 → 骨架 → 路由归属。

对 external/ 下每个应用:
1. 框架探测(venv 里有 django/flask)
2. analyze() 端点数
3. sites 抓取(session 链,含属性写;基准对账)
4. graph 骨架(slots/produces/reads)
5. attribution(端点匹配率、函数覆盖率、真值抽查)
"""

from __future__ import annotations

import ast
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, "/home/landasika/icassp")
from stateforge.analysis.analyzer import analyze              # noqa: E402
from stateforge.analysis.attribution import attribute_routes  # noqa: E402
from stateforge.analysis.static.icfg import build_icfg        # noqa: E402
from stateforge.state.sites import (                          # noqa: E402
    extract_sites, make_entry_matcher)
from stateforge.state.graph import build_state_plane_graph    # noqa: E402

EXT = Path("/home/landasika/icassp/external")
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


def _is_sess(chain):
    if not chain:
        return False
    head, _, last = chain.rpartition(".")
    return last == "session" and not (_DBHINT & set(head.split(".")))


def detect_framework(root: Path) -> str | None:
    for sp in root.glob(".venv/lib/python3*/site-packages"):
        if (sp / "django").is_dir():
            return "django"
        if (sp / "flask").is_dir():
            return "flask"
    return None


def froots(root: Path, fw: str) -> list[Path]:
    out = []
    for sp in root.glob(".venv/lib/python3*/site-packages"):
        names = (["django/contrib"] if fw == "django"
                 else ["flask", "flask_login", "werkzeug"])
        for name in names:
            d = sp / name
            if d.is_dir():
                out.append(d.resolve())
    return out


def raw_baseline(root: Path, skip):
    """基准:session 链全部写形状(下标+变更+属性)。"""
    sub = mut = attr = 0
    for p in root.rglob("*.py"):
        if skip(p) or "/.venv/" in str(p):
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


def run(app: str) -> None:
    root = (EXT / app).resolve()
    fw = detect_framework(root)
    if fw is None:
        print(f"{app:24s} 无 django/flask venv,跳过端点/归属")
        return
    fs = froots(root, fw)

    def skip(p):
        if any(str(p).startswith(str(f) + "/") for f in fs):
            return False
        if {".venv", "site-packages", "__pycache__"} & set(p.parts):
            return True
        if {"test", "tests", "testing", "selftest"} & set(p.parts):
            return True
        return "test" in p.name.lower()

    model = analyze(root, fw)
    entries = {(Path(e.file).name, e.func) for e in model.endpoints}
    scan = extract_sites([root, *fs], skip=skip,
                         is_entry=make_entry_matcher(entries))
    g = build_state_plane_graph(scan)

    sess = [s for s in scan.stores
            if _is_sess(s.chain) and "/.venv/" not in s.file]
    sub, mut, attr = raw_baseline(root, skip)
    base = sub + mut + attr
    rate = f"{len(sess)}/{base}={len(sess)/base:.0%}" if base else "无写点"

    ir = build_icfg(root)
    att = attribute_routes(ir, model.endpoints)
    ep_total = len(model.endpoints)
    matched = len(att.matched_endpoints)
    fn_cov = len(att.routes_of)

    print(f"{app:24s} {fw:6s} 端点{ep_total:4d} 匹配{matched:4d} "
          f"归属函数{fn_cov:5d} | session转移点 {rate:12s} "
          f"| slots {len(g.slots):3d} produces {len(g.produces):4d} "
          f"reads {len(g.reads):4d}")


if __name__ == "__main__":
    apps = sorted(d.name for d in EXT.iterdir() if d.is_dir())
    for app in apps:
        try:
            run(app)
        except Exception as exc:                    # noqa: BLE001
            print(f"{app:24s} 失败 {type(exc).__name__}: {exc}")
