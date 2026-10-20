"""Session-First micro experiment: BASE vs GUIDED vs REUSE.

All arms use the same target, IR, request pool, wall-clock budget and reset
lifecycle.  BASE preloads only independent single-request C10 seeds.  GUIDED
adds the bounded session-guided search before Atheris.  REUSE preloads a
previously verified checkpoint prefix.  Metrics separate coverage from
runtime-verified Session facts and report replay/isolation outcomes.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

from stateforge.analysis.static.icfg import build_icfg
from stateforge.fuzzing.entry_driver import (decode_sequence_seed,
                                             encode_sequence_seed)
from stateforge.state.session_access import OP_UNKNOWN, extract_session_accesses
from stateforge.state.session_index import (build_session_index,
                                            query_session_frontier)
from stateforge.solving.session_replay import (
    checkpoint_from_guided_result, replay_session_checkpoint,
    suffix_without_prefix_reaches_target)

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "stateforge" / "targets" / "session_gate"
TARGET_BLOCK = "app.py::protected::173-173::block::1"
WRITER_BLOCK = "app.py::authenticate::165-165::block::1"
POOL = [
    {"method": "POST", "path": "/authenticate", "form": {"passcode": "wrong"}},
    {"method": "POST", "path": "/authenticate", "form": {"passcode": "valid"}},
    {"method": "GET", "path": "/protected"},
    {"method": "GET", "path": "/protected_truthy"},
    {"method": "GET", "path": "/protected_membership"},
    {"method": "GET", "path": "/protected_subscript"},
    {"method": "POST", "path": "/noise"},
    {"method": "POST", "path": "/logout"},
    {"method": "POST", "path": "/clear"},
]


def _load_target(name="sg_exp"):
    spec = importlib.util.spec_from_file_location(name, GATE / "fuzz_target.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _probe(mod):
    def probe():
        return mod.session_view()
    return probe


def _seed_corpus(corpus: Path) -> None:
    corpus.mkdir(parents=True, exist_ok=True)
    for req in POOL:
        data = encode_sequence_seed([req])
        (corpus / hashlib.sha256(data).hexdigest()[:16]).write_bytes(data)


def _make_checkpoint(out_dir: Path):
    from stateforge.fuzzing.session_guided import SessionGuidedSearch
    mod = _load_target("sg_exp_ckpt")
    ir = build_icfg(GATE)
    accesses = extract_session_accesses(GATE, ir=ir)
    index = build_session_index(accesses)
    frontier = query_session_frontier(ir, index, TARGET_BLOCK, accesses)
    result = SessionGuidedSearch(
        mod.target, ir, TARGET_BLOCK, frontier, POOL, max_execs=60,
        max_steps=3, seed=1, guided=True, reset_hook=mod.reset_state,
        trace_root=GATE, session_probe=_probe(mod)).run()
    ckpt = checkpoint_from_guided_result(result, GATE / "app.py")
    if ckpt is None:
        raise RuntimeError(f"checkpoint creation failed: {result.to_dict()}")
    ckpt.save(out_dir / "checkpoints")
    (out_dir / "reuse_seed.bin").write_bytes(ckpt.prefix_seed())
    return mod, ir, ckpt, result


def _run_driver(arm: str, seed: int, corpus: Path, out: Path,
                seconds: float, pool_path: Path, guided: bool,
                max_runs: int | None = None, *, cost_ledger: bool = False,
                max_target_http_requests: int | None = None,
                run_id: str = "", session_online: bool = False,
                online_exploration: float = 0.0) -> dict:
    cmd = [sys.executable, "-m", "stateforge.fuzzing.entry_driver",
           str(GATE / "fuzz_target.py"), str(out / "ir.json"),
           str(corpus), str(out),
           "--seconds", str(seconds), "--mode", "feedback",
           "--seed", str(seed), "--lifecycle", "reset",
           "--observe-state"]
    if max_runs is not None:
        cmd += ["--max-runs", str(max_runs)]
    if cost_ledger:
        cost_dir = out / "atheris_cost"
        cmd += ["--cost-ledger", "--run-id", run_id or f"{arm}-{seed}",
                "--arm", arm, "--target-block", TARGET_BLOCK,
                "--cost-out", str(cost_dir)]
        if max_target_http_requests is not None:
            cmd += ["--max-target-http-requests",
                    str(max_target_http_requests)]
    if guided:
        cmd += ["--session-guided", "--session-target-block", TARGET_BLOCK,
                "--session-request-pool", str(pool_path),
                "--session-max-execs", "60", "--session-max-steps", "3"]
    if session_online:
        cmd += ["--session-online", "--session-online-exploration",
                str(online_exploration),
                "--session-target-block", TARGET_BLOCK,
                "--session-request-pool", str(pool_path)]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                          timeout=120)
    stderr = proc.stderr or ""
    match = re.search(r"stat::number_of_executed_units:\s*(\d+)", stderr)
    atheris_execs = int(match.group(1)) if match else 0
    state = {}
    gs = out / "global_state.json"
    if gs.exists():
        try:
            state = json.loads(gs.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            state = {}
    blocks = set(state.get("blocks", []))
    evidence = state.get("state_evidence", {}) or {}
    guided_result = {}
    gr = out / "session_guided" / "result.json"
    if gr.exists():
        guided_result = json.loads(gr.read_text(encoding="utf-8"))
    costs = {}
    if cost_ledger:
        from stateforge.runtime.execution_costs import read_costs
        costs = read_costs(out / "atheris_cost")
    online_summary = {}
    op = out / "online" / "online_summary.json"
    if op.exists():
        try:
            online_summary = json.loads(op.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            online_summary = {}
    return {
        "arm": arm,
        "seed": seed,
        "online_summary": online_summary,
        "returncode": proc.returncode,
        "costs": costs,
        "atheris_executions": atheris_execs,
        "guided_search_executions": int(guided_result.get("executions", 0)),
        "total_executions": atheris_execs + int(guided_result.get("executions", 0)),
        "target_covered": TARGET_BLOCK in blocks,
        "writer_covered": WRITER_BLOCK in blocks,
        "verified_atoms": evidence.get("verified_atoms", []),
        "proxy_atoms": evidence.get("proxy_atoms", []),
        "guided_status": guided_result.get("status"),
        "guided_found": guided_result.get("found"),
        "guided_steps": guided_result.get("steps", []),
        "stop_reason": guided_result.get("stop_reason"),
        "corpus_sequences": _corpus_sequences(corpus),
        "stderr_tail": stderr[-400:],
    }


def _corpus_sequences(corpus: Path) -> list[dict]:
    out = []
    if not corpus.exists():
        return out
    for path in sorted(corpus.iterdir()):
        if not path.is_file():
            continue
        try:
            steps = decode_sequence_seed(path.read_bytes())
        except OSError:
            continue
        if steps:
            out.append({"file": path.name, "length": len(steps), "steps": steps})
    return out


def _static_metrics(ir, accesses, index, frontier) -> dict:
    may = [a for a in accesses if a.effect_status == "STATIC_MAY"]
    unknown = [a for a in accesses if a.operation == OP_UNKNOWN]
    return {
        "accesses": len(accesses),
        "static_may": len(may),
        "unknown": len(unknown),
        "static_may_ratio": round(len(may) / len(accesses), 4) if accesses else None,
        "unknown_ratio": round(len(unknown) / len(accesses), 4) if accesses else None,
        "candidate_blocks": list(frontier.candidate_blocks),
        "condition_groups": len(frontier.path_constraint_groups),
        "unresolved": [u.to_dict() for u in frontier.unresolved],
        "partial": frontier.partial,
    }


def run(seeds=(1, 2, 3), seconds=2.0, out_root: Path | None = None) -> dict:
    out_root = out_root or (ROOT / "artifacts" / "session_first")
    raw = out_root / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    ir = build_icfg(GATE)
    ir.to_json(out_root / "program_ir.json")
    accesses = extract_session_accesses(GATE, ir=ir)
    index = build_session_index(accesses)
    frontier = query_session_frontier(ir, index, TARGET_BLOCK, accesses)
    (out_root / "frontier.json").write_text(
        json.dumps(frontier.to_dict(), ensure_ascii=False, indent=1),
        encoding="utf-8")
    pool_path = out_root / "request_pool.json"
    pool_path.write_text(json.dumps(POOL, ensure_ascii=False, indent=1),
                         encoding="utf-8")

    mod, ir, ckpt, ckpt_result = _make_checkpoint(out_root)
    replay = replay_session_checkpoint(
        ckpt, mod.target, ir, reset_hook=mod.reset_state,
        session_probe=mod.session_view, trace_root=GATE)
    suffix = [{"method": "GET", "path": "/protected"}]
    suffix_isolated = not suffix_without_prefix_reaches_target(
        ckpt, mod.target, ir, reset_hook=mod.reset_state,
        trace_root=GATE, suffix_steps=suffix)

    runs = []
    for seed in seeds:
        for arm, guided, extra_seed in (
                ("BASE", False, None),
                ("SESSION-GUIDED", True, None),
                ("SESSION-REUSE", False, "reuse")):
            corpus = raw / f"seed{seed}" / arm / "corpus"
            _seed_corpus(corpus)
            if extra_seed == "reuse":
                (corpus / "verified-prefix").write_bytes(ckpt.prefix_seed())
            out = raw / f"seed{seed}" / arm / "out"
            out.mkdir(parents=True, exist_ok=True)
            (out / "ir.json").write_bytes((out_root / "program_ir.json").read_bytes())
            record = _run_driver(arm, seed, corpus, out, seconds, pool_path,
                                 guided=guided)
            record["replay_success"] = replay.replay_success
            record["replay_status"] = replay.status
            record["replay_values"] = replay.values
            record["suffix_without_prefix_isolated"] = suffix_isolated
            runs.append(record)

    summary = {
        "seconds": seconds,
        "seeds": list(seeds),
        "request_pool_size": len(POOL),
        "target_block": TARGET_BLOCK,
        "writer_block": WRITER_BLOCK,
        "checkpoint": ckpt.to_dict(),
        "checkpoint_creation": {
            "status": ckpt_result.status,
            "executions": ckpt_result.executions,
            "steps": ckpt_result.steps,
        },
        "replay": replay.to_dict(),
        "suffix_without_prefix_isolated": suffix_isolated,
        "static": _static_metrics(ir, accesses, index, frontier),
        "runs": runs,
        "aggregate": _aggregate(runs),
    }
    (out_root / "session_experiment.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_root / "SESSION_EXPERIMENT.md").write_text(
        _render_markdown(summary), encoding="utf-8")
    return summary


def _aggregate(runs: list[dict]) -> dict:
    arms = {}
    for arm in ("BASE", "SESSION-GUIDED", "SESSION-REUSE"):
        subset = [r for r in runs if r["arm"] == arm]
        arms[arm] = {
            "target_reached": sum(1 for r in subset if r["target_covered"]),
            "writer_reached": sum(1 for r in subset if r["writer_covered"]),
            "verified_session": sum(1 for r in subset if r["verified_atoms"]),
            "proxy_only": sum(1 for r in subset
                              if r["proxy_atoms"] and not r["verified_atoms"]),
            "mean_total_executions": round(
                sum(r["total_executions"] for r in subset) / len(subset), 1)
            if subset else 0,
            "mean_atheris_executions": round(
                sum(r["atheris_executions"] for r in subset) / len(subset), 1)
            if subset else 0,
            "mean_sequence_length": round(
                sum(sum(s["length"] for s in r["corpus_sequences"])
                    / max(1, len(r["corpus_sequences"])) for r in subset)
                / len(subset), 2) if subset else 0,
            "runs": len(subset),
        }
    return arms


def _render_markdown(s: dict) -> str:
    lines = [
        "# Session-First 实验（BASE / GUIDED / REUSE）", "",
        f"- seeds: {s['seeds']}；wall-clock/arm: {s['seconds']}s；"
        f"request pool: {s['request_pool_size']} 个独立单请求",
        f"- target: `{s['target_block']}`",
        f"- checkpoint: `{s['checkpoint']['checkpoint_id']}`，"
        f"replay={s['replay']['status']}，"
        f"values={s['replay']['values']}",
        f"- suffix-without-prefix isolated: {s['suffix_without_prefix_isolated']}",
        "", "## 逐臂汇总", "",
        "| arm | target | writer | verified session | proxy-only | mean total exec | mean Atheris exec | mean seq len |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for arm, a in s["aggregate"].items():
        lines.append(
            f"| {arm} | {a['target_reached']}/{a['runs']} | "
            f"{a['writer_reached']}/{a['runs']} | "
            f"{a['verified_session']}/{a['runs']} | "
            f"{a['proxy_only']}/{a['runs']} | {a['mean_total_executions']} | "
            f"{a['mean_atheris_executions']} | {a['mean_sequence_length']} |")
    lines += ["", "## 静态 Session 访问", "",
              f"- accesses={s['static']['accesses']}，"
              f"STATIC_MAY={s['static']['static_may']}，"
              f"UNKNOWN={s['static']['unknown']}，"
              f"candidate blocks={s['static']['candidate_blocks']}",
              "", "## 逐次运行", "",
              "| arm | seed | target | writer | verified | total exec | Atheris | guided |",
              "|---|---:|---:|---:|---:|---:|---:|---|"]
    for r in s["runs"]:
        lines.append(
            f"| {r['arm']} | {r['seed']} | {r['target_covered']} | "
            f"{r['writer_covered']} | {r['verified_atoms']} | "
            f"{r['total_executions']} | {r['atheris_executions']} | "
            f"{r['guided_status']} |")
    lines += ["", "原始产物：`raw/`；机器可读汇总：`session_experiment.json`。"]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", default="1,2,3")
    parser.add_argument("--seconds", type=float, default=2.0)
    parser.add_argument("--out", default=None)
    parser.add_argument("--protocol", default=None,
                        help="SS-06: 可选 ss06-v1 四臂公平消融协议")
    parser.add_argument("--arms", default="SINGLE,SEQUENCE,STATE_GUIDED,STATE_REUSE")
    parser.add_argument("--total-target-exec-budget", type=int, default=120)
    parser.add_argument("--max-steps", type=int, default=3)
    parser.add_argument("--session-online", action="store_true",
                        help="SS-07C: 在线 Session 反馈(仅 STATE_GUIDED)")
    parser.add_argument("--session-online-exploration", type=float,
                        default=0.0)
    args = parser.parse_args()
    seeds = tuple(int(x) for x in args.seeds.split(",") if x.strip())
    if args.protocol:
        if args.protocol != "ss06-v1":
            parser.error("仅支持 --protocol ss06-v1")
        from stateforge.evaluation.session_ss06 import run_protocol
        arms = tuple(x.strip() for x in args.arms.split(",") if x.strip())
        run_protocol(seeds=seeds, seconds=args.seconds,
                     total_target_exec_budget=args.total_target_exec_budget,
                     max_steps=args.max_steps, arms=arms,
                     out_root=Path(args.out) if args.out else None,
                     online=args.session_online,
                     online_exploration=args.session_online_exploration)
        return 0
    run(seeds=seeds, seconds=args.seconds,
        out_root=Path(args.out) if args.out else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
