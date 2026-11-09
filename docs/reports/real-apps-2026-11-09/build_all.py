"""Build the ICFG for every project under external/ and report anomalies."""
import json
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, "/home/landasika/icassp")

from stateforge.analysis.static.icfg import build_icfg

ROOT = Path("/home/landasika/icassp/external")
out = []
for project in sorted(p for p in ROOT.iterdir() if p.is_dir()):
    if project.name.startswith("."):
        continue
    started = time.time()
    row = {"project": project.name}
    try:
        ir = build_icfg(project)
        row.update(
            files=len({b.file for b in ir.blocks.values()}),
            functions=len(ir.functions),
            blocks=len(ir.blocks),
            edges=len(ir.edges),
            unresolved=len(ir.metadata.get("unresolved_calls", [])),
            seconds=round(time.time() - started, 2),
            status="ok",
        )
    except Exception as exc:                     # noqa: BLE001 - report, don't abort
        row.update(status="error", error=f"{type(exc).__name__}: {exc}",
                   trace=traceback.format_exc(limit=3),
                   seconds=round(time.time() - started, 2))
    out.append(row)
    print(json.dumps(row, ensure_ascii=False), flush=True)

Path("/tmp/sf_all_static.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
print("done", len(out))
