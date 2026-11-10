"""量:通用位点提取器在真实应用 + 框架切片上的产出(v2)。

修正 v1 两处:
- app 根 rglob 会把 .venv 全扫进来 → skip 对框架切片豁免、对应用侧排除
- 纯共享判定放进 kwargs/context 等请求内字典 → 加 is_entry 资格过滤
"""

from __future__ import annotations

import ast
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, "/home/landasika/icassp")

from stateforge.state.sites import extract_sites  # noqa: E402
from stateforge.analysis.analyzer import analyze   # noqa: E402


def venv_site(app: str) -> Path | None:
    for p in sorted(Path(f"/home/landasika/icassp/external/{app}").glob(
            ".venv/lib/python3*/site-packages")):
        return p
    return None


def framework_roots(app: str) -> list[Path]:
    sp = venv_site(app)
    if sp is None:
        return []
    out = []
    for name in ("django/contrib", "flask", "flask_login", "werkzeug"):
        d = sp / name
        if d.is_dir():
            out.append(d.resolve())
    return out


def run(app: str, framework: str) -> None:
    root = Path(f"/home/landasika/icassp/external/{app}").resolve()
    froots = framework_roots(app)

    def skip(p: Path) -> bool:
        rp = p.resolve()
        if any(str(rp).startswith(str(f) + "/") for f in froots):
            return False                       # 框架切片:不排除
        if {".venv", "site-packages", "__pycache__"} & set(p.parts):
            return True                        # 应用侧:排除依赖树
        if {"test", "tests", "testing", "selftest"} & set(p.parts):
            return True
        return "test" in p.name.lower()

    model = analyze(root, framework)
    entries = {(Path(e.file).name, e.func) for e in model.endpoints}
    is_entry = lambda fn, file: (Path(file).name, fn) in entries  # noqa: E731

    scan = extract_sites([root, *froots], skip=skip, is_entry=is_entry)

    def is_app(f: str) -> bool:
        return "/.venv/" not in f

    app_stores = [s for s in scan.stores if is_app(s.file)]
    fw_stores = [s for s in scan.stores if not is_app(s.file)]
    app_guards = [g for g in scan.guards if is_app(g.file)]
    fw_guards = [g for g in scan.guards if not is_app(g.file)]

    attributed = [g for g in app_guards
                  if (Path(g.file).name, g.function) in entries]
    guard_eps = {(Path(g.file).name, g.function) for g in attributed}

    # 装饰器视图(应用代码)
    dec: set[tuple[str, str]] = set()
    for p in root.rglob("*.py"):
        if skip(p):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.decorator_list:
                dec.add((p.name, node.name))

    ep_all = {(Path(e.file).name, e.func) for e in model.endpoints}
    ep_guard = ep_all & guard_eps
    ep_dec = ep_all & dec
    covered = ep_guard | ep_dec

    print(f"===== {app} (切片 {[f.name for f in froots] or '无'})")
    print(f"  store: 应用 {len(app_stores)}  框架 {len(fw_stores)}  "
          f"丢弃 {scan.dropped_unshared}")
    print(f"  key 证据: {dict(Counter(s.key_status for s in scan.stores))}")
    print(f"  top 链: {Counter(s.chain for s in scan.stores).most_common(5)}")
    print(f"  守卫: 应用 {len(app_guards)}  框架 {len(fw_guards)}")
    if app_guards:
        print(f"  守卫→端点归属: {len(attributed)}/{len(app_guards)} "
              f"({len(attributed)/len(app_guards):.0%})")
    print(f"  端点 {len(ep_all)}: 自带守卫 {len(ep_guard)}  带装饰器 "
          f"{len(ep_dec)}  并集 {len(covered)} ({len(covered)/len(ep_all):.0%})"
          if ep_all else "  端点 0")

    if app == "healthchecks":
        for s in fw_stores:
            if s.key == "_auth_user_id" and s.op == "write":
                print(f"  [验证] Django 写点: {Path(s.file).name}:{s.line} "
                      f"{s.slot} in {s.function} ({s.key_status})")
    if app == "CTFd":
        for s in app_stores:
            if s.chain == "session" and s.key in ("id", "hash"):
                print(f"  [验证] CTFd 自管写点: {Path(s.file).name}:{s.line} "
                      f"{s.slot} in {s.function}")


if __name__ == "__main__":
    run("healthchecks", "django")
    run("CTFd", "flask")
    run("microblog", "flask")
