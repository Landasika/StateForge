"""Phase-1 验收:静态链路 + 动态 balance=50 请求 + frontier(spec §4 P1-8)。

若本文件任何断言失败,Phase-1 不算完成。
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from stateforge.dynamic.tracer import TraceSession
from stateforge.feedback.engine import compute_feedback
from stateforge.static.icfg import build_icfg

FIXTURE = Path(__file__).parents[2] / "fixtures" / "buy_app"


def _load_module():
    spec = importlib.util.spec_from_file_location("buy_app_app", FIXTURE / "app.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["buy_app_app"] = mod
    spec.loader.exec_module(mod)
    return mod


def _trace_buy(balance: int):
    mod = _load_module()
    mod.USERS["bee"] = SimpleNamespace(balance=balance)
    client = mod.app.test_client()
    with TraceSession(root=str(FIXTURE)) as session:
        with session.request() as rid:
            resp = client.post("/buy", headers={"X-User": "bee"})
            events = list(session.events)
    assert resp.get_data(as_text=True) == ("insufficient" if balance <= 100
                                           else "charged")
    return events


def test_static_chain():
    ir = build_icfg(FIXTURE)
    fns = set(ir.functions)
    assert {"app.buy", "app.get_user", "app.log_access",
            "app.checkout", "app.charge"} <= fns
    cs = [b for b in ir.blocks.values() if b.kind == "callsite"]
    rs = [b for b in ir.blocks.values() if b.kind == "returnsite"]
    assert len(cs) >= 4 and len(rs) >= 4          # buy×3 + checkout×1
    assert any(e.kind == "return" for e in ir.edges)
    # request.headers.get 是外部属性调用:必须落 unresolved,不得出现假 call 边
    assert ir.metadata["unresolved_calls"]


def test_dynamic_frontier():
    ir = build_icfg(FIXTURE)
    events = _trace_buy(50)                       # 余额不足:只走 false 分支
    report = compute_feedback(ir, events)
    assert report.overlay.covered_blocks
    assert report.request_count == 1
    frontier_srcs = {f.src for f in report.frontiers}
    # checkout 条件块的 true 边(→ charge 调用链)必为 frontier
    checkout_true = [s for s in frontier_srcs
                     if ir.blocks[s].function == "checkout"]
    assert checkout_true, f"balance=50 时 checkout true 分支必为 frontier,got {frontier_srcs}"
    # buy 的 401 分支同样未走(bee 用户存在)
    buy_frontier = [s for s in frontier_srcs if ir.blocks[s].function == "buy"]
    assert buy_frontier


def test_cli_experiment_e2e(tmp_path):
    events = _trace_buy(50)
    # JSONL 产物格式 = atheris_runner 的"一行一输入记录"(request_id + events 列表)
    trace = tmp_path / "trace.jsonl"
    trace.write_text(json.dumps({
        "request_id": events[0].request_id if events else 0,
        "events": [{"request_id": e.request_id, "index": e.index, "file": e.file,
                    "function": e.function, "line": e.line,
                    "event_type": e.event_type} for e in events],
    }), encoding="utf-8")
    out = tmp_path / "exp"
    subprocess.run([sys.executable, "-m", "stateforge.cli", "experiment",
                    str(FIXTURE), "--out", str(out), "--trace", str(trace)],
                   capture_output=True, text=True, check=True)
    fb = json.loads((out / "feedback.json").read_text(encoding="utf-8"))
    assert fb["frontiers"] and fb["request_count"] >= 1
    # viz 叠图端到端
    subprocess.run([sys.executable, "-m", "stateforge.cli", "viz",
                    str(out / "program_ir.json"), str(out / "feedback.json"),
                    "--out", str(tmp_path / "cov.dot")],
                   capture_output=True, text=True, check=True)
    assert "frontier" in (tmp_path / "cov.dot").read_text(encoding="utf-8")
