"""对账:静态 session 转移点(sites.py) vs 真实执行(CTFd 请求流)。"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, "/home/landasika/icassp")
from stateforge.state.sites import extract_sites   # noqa: E402
from stateforge.analysis.analyzer import analyze    # noqa: E402

ROOT = Path("/home/landasika/icassp/external/CTFd").resolve()
TRACE = Path("/tmp/sf_sess_trace_ctfd.json")
REQS = Path("/tmp/sf_sess_requests.json")

sp = next(ROOT.glob(".venv/lib/python3*/site-packages"))
froots = [d for d in (sp / "flask", sp / "werkzeug") if d.is_dir()]


def skip(p: Path) -> bool:
    if any(str(p).startswith(str(f) + "/") for f in froots):
        return False
    if {".venv", "site-packages", "__pycache__"} & set(p.parts):
        return True
    if {"test", "tests"} & set(p.parts):
        return True
    return "test" in p.name.lower()


model = analyze(ROOT, "flask")
entries = {(Path(e.file).name, e.func) for e in model.endpoints}
scan = extract_sites([ROOT, *froots], skip=skip,
                     is_entry=lambda fn, f: (Path(f).name, fn) in entries)

sess = [s for s in scan.stores
        if s.chain == "session" and str(ROOT) in s.file]

events = json.loads(TRACE.read_text())
exec_lines = {(e["file"], e["line"]) for e in events}

hits = [s for s in sess if (s.file, s.line) in exec_lines]

print(f"静态 session 转移点(CTFd 应用代码): {len(sess)}")
print(f"本次请求流执行到的: {len(hits)}")
for s in sorted(sess, key=lambda s: (s.file, s.line)):
    mark = "执行" if (s.file, s.line) in exec_lines else "未执行"
    print(f"  [{mark}] {Path(s.file).relative_to(ROOT)}:{s.line}"
          f"  {s.slot:<28} {s.op:<10} fn={s.function}")

# 观测到的转移记录(请求级快照差)→ 静态点 join
rows = json.loads(REQS.read_text())
print("\n=== 观测转移记录(快照差)→ 静态点 join")
key_hits = {s.key for s in hits}
for r in rows:
    b, a = r["before"], r["after"]
    added = [k for k in a if k not in b]
    gone = [k for k in b if k not in a]
    chg = [k for k in a if k in b and b[k] != a[k]]
    if not (added or gone or chg):
        continue
    parts = []
    for k in added + chg:
        src = "静态命中" if k in key_hits else "静态缺口"
        parts.append(f"{k}({src})")
    for k in gone:
        parts.append(f"-{k}({'静态命中' if k in key_hits else '静态缺口'})")
    print(f"  {r['status']:>4} {r['label']}: {' '.join(parts)}")

# 缺口检查:执行到的"session 属性写"(不在静态集合,提取器只认下标)
auth = str(ROOT / "CTFd/utils/security/auth.py")
print("\n=== auth.py 执行行(10-32)对照")
for ln in range(10, 33):
    ran = (auth, ln) in exec_lines
    static = any(s.file == auth and s.line == ln for s in sess)
    if ran or static:
        tag = "执行" if ran else "    "
        tag += "+静态" if static else "     "
        print(f"  {tag}  auth.py:{ln}")

# 各请求触发的转移点数
print("\n=== 每请求执行的转移点(按事件顺序归属略,给总量)")
c = Counter(s.op for s in hits)
print(f"  命中按 op: {dict(c)}")
