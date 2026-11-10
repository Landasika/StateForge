"""joiner(ci-venv):静态转移点 + 驱动步骤 → 观测状态机,并验证。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/home/landasika/icassp")
from stateforge.state.sites import (             # noqa: E402
    extract_sites, make_entry_matcher)
from stateforge.state.join import observe        # noqa: E402
from stateforge.state.machine import StateMachine   # noqa: E402
from stateforge.analysis.analyzer import analyze # noqa: E402

ROOT = Path("/home/landasika/icassp/external/CTFd").resolve()
OUT = Path("/home/landasika/icassp/docs/reports/state-sites-2026-11-10")

sp = next(ROOT.glob(".venv/lib/python3*/site-packages"))
froots = [d.resolve() for d in (sp / "flask", sp / "werkzeug") if d.is_dir()]


def skip(p: Path) -> bool:
    if any(str(p).startswith(str(f) + "/") for f in froots):
        return False
    if {".venv", "site-packages", "__pycache__"} & set(p.parts):
        return True
    if {"test", "tests", "testing", "selftest"} & set(p.parts):
        return True
    return "test" in p.name.lower()


model = analyze(ROOT, "flask")
entries = {(Path(e.file).name, e.func) for e in model.endpoints}
scan = extract_sites([ROOT, *froots], skip=skip,
                     is_entry=make_entry_matcher(entries))
stores = scan.stores
print(f"静态转移点: {len(stores)}")

steps = json.loads(Path("/tmp/sf_join_steps.json").read_text())
machine = StateMachine()
n_edges = n_none = 0
for st in steps:
    executed = {(f, ln) for f, ln in st["executed"]}
    pts = [s for s in stores if (s.file, s.line) in executed]
    t = observe(st["trigger"], st["before"], st["after"], pts,
                chains={"session"})
    if t is None:
        n_none += 1
        continue
    new = machine.add(t)
    n_edges += int(new)
    print(f'  {st["status"]:>3} {st["label"]:<20} → {st["trigger"]}'
          f'  +{t.delta.added} -{t.delta.removed} ~{t.delta.changed}'
          f'  归因 {len(t.points)} 点'
          f'  未归因 {t.unattributed or "无"}')

print(f"\n观测转移: 新边 {n_edges},无变化(不产边) {n_none}")
print(f"状态: {len(machine.states)}")
for e in machine.edges:
    print(f"  {sorted(e.from_state) or '∅'} --{e.trigger}--> "
          f"{sorted(e.to_state) or '∅'}  points={e.points}")

# 验收查询:怎么让 session 里的 id 出现
print("\n[查询] 哪些请求能建立 session[id]:")
for e in machine.triggers_producing("id"):
    print(f"  → {e.trigger} (证据 {e.points})")

(OUT / "ctfd_state_machine.json").write_text(
    json.dumps(machine.to_dict(), ensure_ascii=False, indent=1),
    encoding="utf-8")
(OUT / "ctfd_state_machine.dot").write_text(
    machine.to_dot("CTFd observed state machine"), encoding="utf-8")
subprocess.run(["dot", "-Tpng",
                str(OUT / "ctfd_state_machine.dot"),
                "-o", str(OUT / "ctfd_state_machine.png")], check=True)
print(f"\n[out] {OUT / 'ctfd_state_machine.png'}")
