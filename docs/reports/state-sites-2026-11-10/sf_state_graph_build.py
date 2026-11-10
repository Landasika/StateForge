"""在真实应用上构建状态面图并渲染 DOT/PNG。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, "/home/landasika/icassp")
from stateforge.state.sites import (            # noqa: E402
    extract_sites, make_entry_matcher)
from stateforge.state.graph import build_state_plane_graph   # noqa: E402
from stateforge.analysis.analyzer import analyze             # noqa: E402

OUT = Path("/home/landasika/icassp/docs/reports/state-sites-2026-11-10")


def froots_of(app):
    sp = next(Path(f"/home/landasika/icassp/external/{app}")
              .glob(".venv/lib/python3*/site-packages"))
    out = []
    for name in ("django/contrib", "flask", "flask_login", "werkzeug"):
        d = sp / name
        if d.is_dir():
            out.append(d.resolve())
    return out


def run(app: str, framework: str) -> None:
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
    scan = extract_sites([root, *fs], skip=skip,
                         is_entry=make_entry_matcher(entries))
    g = build_state_plane_graph(scan)

    ro = [s for s in g.slots.values() if not s.written]
    print(f"===== {app}: slots {len(g.slots)}(只读 {len(ro)})"
          f"  actions {len(g.actions)}  produces {len(g.produces)}"
          f"  reads {len(g.reads)}")

    dot = g.to_dot(f"{app} state plane")
    (OUT / f"{app}_state_plane.dot").write_text(dot, encoding="utf-8")
    png = OUT / f"{app}_state_plane.png"
    import subprocess
    subprocess.run(["dot", "-Tpng", str(OUT / f"{app}_state_plane.dot"),
                    "-o", str(png)], check=True)

    if app == "CTFd":
        # 已知真值核对:login_user 写 id/nonce/hash/permanent;
        # logout_user 清库;authed/get_current_user 读 id/hash
        for fn in ("login_user", "logout_user", "authed", "get_current_user"):
            pro = [(p.slot, p.op) for p in g.produces if p.action == fn]
            rd = [r.slot for r in g.reads if r.action == fn]
            print(f"  {fn}: produces={pro} reads={sorted(set(rd))}")


if __name__ == "__main__":
    run("CTFd", "flask")
    run("healthchecks", "django")
