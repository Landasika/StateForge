"""SF-Micro:结构管线微型真值基准(评审§19,H8)。

每个用例 = stateforge/fixtures/micro/<name>/src.py + expected_icfg.json
(人工核准的归一化边真值)。可选 expected_trace.json + src.run() 驱动函数
= 动态 BB 序列真值。

归一化(与块 ID 无关,跨机器稳定):
  节点键   entry/exit → ``func::entry`` / ``func::exit``;
           其余 → ``func:start_line:kind``
  CFG 边   (src_key, true|false|back|break|continue|flow, dst_key)
           ——合成 callsite/returnsite 的 flow 边折叠进调用边,不单独出现
  调用边   (``caller:line:call``, call, ``callee::entry``)
  返回边   (``callee::exit``, return, ``caller:line:call``)

输出 per-case / 全量的 CFG/CALL/RETURN precision & recall(真值语义口径,
已知限制的用例由 expected 文件内 "allow" 声明阈值)。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from stateforge.core.ir import ProgramIR

MICRO_ROOT = Path(__file__).parents[1] / "fixtures" / "micro"
_SYNTH = {"callsite", "returnsite"}


def _node_key(ir: ProgramIR, bid: str) -> str:
    b = ir.blocks[bid]
    if b.kind == "entry":
        return f"{b.function}::entry"
    if b.kind == "exit":
        return f"{b.function}::exit"
    if b.kind in _SYNTH:                       # 合成节点统一按调用锚点命名
        return f"{b.function}:{b.start_line}:call"
    return f"{b.function}:{b.start_line}:{b.kind}"


def normalize(ir: ProgramIR) -> set[tuple[str, str, str]]:
    out: set[tuple[str, str, str]] = set()
    for e in ir.edges:
        src_b, dst_b = ir.blocks.get(e.src), ir.blocks.get(e.dst)
        if src_b is None or dst_b is None:
            continue
        if e.kind == "call" and src_b.kind == "callsite":
            out.add((f"{src_b.function}:{e.callsite_line}:call", "call",
                     f"{dst_b.function}::entry"))
        elif e.kind == "return" and dst_b.kind == "returnsite":
            out.add((f"{src_b.function}::exit", "return",
                     f"{dst_b.function}:{e.callsite_line}:call"))
        elif e.kind in ("flow", "true", "false"):
            kind = e.kind if e.kind in ("true", "false") else (e.condition or "flow")
            if dst_b.kind == "callsite":
                continue                      # 链内部(B→CS / RS→CS):链语义已由调用边表达
            if src_b.kind == "callsite":
                continue
            if src_b.kind == "returnsite":
                # 调用返回后的续边(含 loop back / 分支续),重锚到调用点
                out.add((f"{src_b.function}:{src_b.start_line}:call", kind,
                         _node_key(ir, e.dst)))
            else:
                out.add((_node_key(ir, e.src), kind, _node_key(ir, e.dst)))
    return out


def _by_kind(edges: set[tuple[str, str, str]]):
    groups = {"CFG": set(), "CALL": set(), "RETURN": set()}
    for edge in edges:
        groups["CALL" if edge[1] == "call" else
               "RETURN" if edge[1] == "return" else "CFG"].add(edge)
    return groups


def compare_case(name: str, ir: ProgramIR) -> dict:
    expected_raw = json.loads((MICRO_ROOT / name / "expected_icfg.json")
                              .read_text(encoding="utf-8"))
    expected = {tuple(e) for e in expected_raw["edges"]}
    produced = normalize(ir)
    eg, pg = _by_kind(expected), _by_kind(produced)
    metrics = {}
    for kind in ("CFG", "CALL", "RETURN"):
        inter = len(eg[kind] & pg[kind])
        metrics[kind] = {
            "precision": round(inter / len(pg[kind]), 4) if pg[kind] else 1.0,
            "recall": round(inter / len(eg[kind]), 4) if eg[kind] else 1.0,
            "produced": len(pg[kind]), "expected": len(eg[kind]),
        }
    metrics["allow"] = expected_raw.get("allow", {})
    expected_all = eg["CFG"] | eg["CALL"] | eg["RETURN"]
    metrics["missing"] = sorted(expected_all - produced)
    metrics["phantom"] = sorted(produced - expected_all)
    return metrics


def run_static(root: Path = MICRO_ROOT) -> dict:
    from stateforge.static.icfg import build_icfg
    report = {}
    for case in sorted(p.name for p in root.iterdir() if p.is_dir()):
        expected_path = root / case / "expected_icfg.json"
        if not expected_path.exists():
            continue
        ir = build_icfg(root / case)
        report[case] = compare_case(case, ir)
    return report


def run_dynamic(root: Path = MICRO_ROOT) -> dict:
    """有 expected_trace.json 的用例:执行 run() 采样并比对 BB 序列。"""
    from stateforge.dynamic.mapper import map_trace
    from stateforge.dynamic.tracer import TraceSession

    out = {}
    for case in sorted(p.name for p in root.iterdir() if p.is_dir()):
        tp = root / case / "expected_trace.json"
        if not tp.exists():
            continue
        spec = importlib.util.spec_from_file_location(f"micro_{case}",
                                                      root / case / "src.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"micro_{case}"] = mod
        spec.loader.exec_module(mod)
        with TraceSession(root=str(root / case)) as session:
            with session.request():
                mod.run(*json.loads((root / case / "input.json")
                                    .read_text(encoding="utf-8")))
            events = list(session.events)
        from stateforge.static.icfg import build_icfg
        ir = build_icfg(root / case)
        mapped = map_trace(events, ir)
        seq = [_node_key(ir, b) for b in mapped.blocks]
        want = json.loads(tp.read_text(encoding="utf-8"))["blocks"]
        match = sum(1 for a, b in zip(seq, want) if a == b)
        out[case] = {"accuracy": round(match / max(len(want), len(seq), 1), 4),
                     "got": seq, "want": want}
    return out


def evaluate(root: Path = MICRO_ROOT, out: Path | None = None) -> dict:
    report = {"static": run_static(root), "dynamic": run_dynamic(root)}
    lines = ["case                 CFG-P  CFG-R  CALL-P CALL-R RET-P  RET-R"]
    for case, m in report["static"].items():
        lines.append(f"{case:20s} "
                     f"{m['CFG']['precision']:.2f}   {m['CFG']['recall']:.2f}   "
                     f"{m['CALL']['precision']:.2f}    {m['CALL']['recall']:.2f}   "
                     f"{m['RETURN']['precision']:.2f}   {m['RETURN']['recall']:.2f}")
    for case, d in report["dynamic"].items():
        lines.append(f"{case:20s} trace-accuracy={d['accuracy']:.2f}")
    text = "\n".join(lines)
    print(text)
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(report, indent=2, ensure_ascii=False),
                             encoding="utf-8")
        print(f"[out] {out}")
    return report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="SF-Micro ground-truth benchmark")
    parser.add_argument("--out", default="results/micro/metrics.json")
    args = parser.parse_args()
    raise SystemExit(0 if evaluate(out=args.out) else 1)
