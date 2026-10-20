"""SS-06A: fair four-arm Session ablation protocol.

Arms differ only in the declared mechanism:

* SINGLE       Atheris + independent single-request corpus.
* SEQUENCE     same structural search as STATE_GUIDED, but ``guided=False``
               (no Session writer/condition weighting); found sequence is
               injected before Atheris.
* STATE_GUIDED same search with Session frontier guidance.
* STATE_REUSE  reuse the independently verified preparation prefix from
               STATE_GUIDED for two suffixes; every suffix starts from a cold
               reset and re-executes the prefix.

All arms share the target, pool, max sequence length, random seed, reset
policy, wall-clock limit and a target-call/input budget account.  Search
bootstrap, checkpoint validation and prefix replays are counted; they are not
free.  The numbers are a functional comparison, not a significance claim.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
import sys
import time
from pathlib import Path

from stateforge.analysis.static.icfg import build_icfg
from stateforge.fuzzing.session_guided import SessionGuidedSearch
from stateforge.runtime.execution_costs import (ExecutionCostLedger,
                                                ExecutionCosts)
from stateforge.state.session_access import extract_session_accesses
from stateforge.state.session_index import (build_session_index,
                                            query_session_frontier)
from stateforge.solving.session_replay import (
    preparation_checkpoint_from_guided_result,
    replay_session_checkpoint,
    suffix_without_prefix_reaches_target,
)

from stateforge.evaluation.session_experiment import (
    GATE,
    POOL,
    TARGET_BLOCK,
    WRITER_BLOCK,
    _corpus_sequences,
    _load_target,
    _run_driver,
    _seed_corpus,
    _probe,
)

ROOT = Path(__file__).resolve().parents[2]
ARMS = ("SINGLE", "SEQUENCE", "STATE_GUIDED", "STATE_REUSE")
TRUTHY_BLOCK = "app.py::protected_truthy::182-182::block::1"
PROTOCOL = "ss06-v1"


def _revision() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:                              # noqa: BLE001
        return "unknown"


def _versions() -> dict:
    import sys as _sys
    from importlib.metadata import PackageNotFoundError, version
    out = {"python": _sys.version.split()[0]}
    for name in ("Flask", "Werkzeug", "pytest", "atheris"):
        try:
            out[name.lower()] = version(name)
        except PackageNotFoundError:
            out[name.lower()] = "missing"
    return out


def _bootstrap(arm: str, seed: int, ir, frontier, out: Path, corpus: Path,
               max_execs: int, max_steps: int, ledger: ExecutionCostLedger):
    mod = _load_target(f"ss06_{arm}_{seed}")
    guided = arm == "STATE_GUIDED"
    target = ledger.wrap_target(mod.target, phase="bootstrap")
    reset = ledger.wrap_reset(mod.reset_state)
    probe = ledger.wrap_observer(_probe(mod))
    result = SessionGuidedSearch(
        target, ir, TARGET_BLOCK, frontier, POOL,
        max_execs=max_execs, max_steps=max_steps, seed=seed, guided=guided,
        reset_hook=reset, trace_root=GATE,
        session_probe=probe).run()
    if result.seed:
        cid = __import__("hashlib").sha256(result.seed).hexdigest()[:16]
        (corpus / cid).write_bytes(result.seed)
    return mod, result


def _prep_from_c(seed: int, ir, frontier, result, mod,
                 ledger: ExecutionCostLedger):
    target = ledger.wrap_target(mod.target, phase="checkpoint")
    reset = ledger.wrap_reset(mod.reset_state)
    probe = ledger.wrap_observer(mod.session_view)
    return preparation_checkpoint_from_guided_result(
        result, GATE / "app.py", ir, target=target,
        reset_hook=reset, session_probe=probe, trace_root=GATE)


def _base_record(seed: int, arm: str) -> dict:
    return {
        "seed": seed,
        "target": TARGET_BLOCK,
        "arm": arm,
        "bootstrap_execs": 0,
        "atheris_execs": 0,
        "checkpoint_validation_execs": 0,
        "prefix_replay_execs": 0,
        "total_target_execs": 0,
        "elapsed_wall_seconds": None,
        "analysis_wall_seconds": 0.0,
        "target_http_requests": 0,
        "sequence_attempts": 0,
        "atheris_inputs": 0,
        "bootstrap_http_requests": 0,
        "checkpoint_validation_http_requests": 0,
        "prefix_replay_http_requests": 0,
        "suffix_http_requests": 0,
        "observer_calls": 0,
        "reset_calls": 0,
        "scheduler_decisions": 0,
        "execution_costs": {},
        "target_reached": False,
        "session_condition_verified": False,
        "time_to_first_target": None,
        "executions_to_first_target": None,
        "sequence_length": None,
        "prefix_length": None,
        "suffix_new_blocks": [],
        "failures": [],
        "source_revision": _revision(),
        "http_requests_executed": 0,
        "sequence_evaluations": 0,
        "stop_reason": None,
    }


def _run_atheris(seed: int, arm: str, corpus: Path, out: Path, seconds: float,
                 pool_path: Path, max_runs: int, *,
                 max_requests: int | None = None,
                 run_id: str = "") -> dict:
    t0 = time.monotonic()
    raw = _run_driver(arm, seed, corpus, out, seconds, pool_path,
                      guided=False, max_runs=max_runs, cost_ledger=True,
                      max_target_http_requests=max_requests,
                      run_id=run_id)
    rec = _base_record(seed, arm)
    costs = raw.get("costs", {}) or {}
    rec.update({
        "atheris_execs": raw["atheris_executions"],
        "total_target_execs": costs.get("target_http_requests", 0),
        "target_http_requests": costs.get("target_http_requests", 0),
        "sequence_attempts": costs.get("sequence_attempts", 0),
        "atheris_inputs": costs.get("atheris_inputs", 0),
        "bootstrap_http_requests": costs.get("bootstrap_http_requests", 0),
        "checkpoint_validation_http_requests": costs.get(
            "checkpoint_validation_http_requests", 0),
        "prefix_replay_http_requests": costs.get(
            "prefix_replay_http_requests", 0),
        "suffix_http_requests": costs.get("suffix_http_requests", 0),
        "observer_calls": costs.get("observer_calls", 0),
        "reset_calls": costs.get("reset_calls", 0),
        "scheduler_decisions": costs.get("scheduler_decisions", 0),
        "execution_costs": costs,
        "elapsed_wall_seconds": round(time.monotonic() - t0, 3),
        "target_reached": raw["target_covered"],
        "writer_covered": raw["writer_covered"],
        "session_condition_verified": bool(raw["verified_atoms"]),
        "verified_atoms": raw["verified_atoms"],
        "proxy_atoms": raw["proxy_atoms"],
        "guided_status": raw.get("guided_status"),
        "guided_found": raw.get("guided_found"),
        "sequence_length": (len(raw["guided_steps"])
                           if raw.get("guided_steps") else None),
        "corpus_sequences": raw["corpus_sequences"],
        "source_revision": _revision(),
        "http_requests_executed": costs.get("target_http_requests", 0),
        "failures": [] if raw["returncode"] == 0 else [
            f"driver-returncode-{raw['returncode']}",
            raw.get("stderr_tail", "")],
        "stop_reason": raw.get("stop_reason"),
    })
    return rec


def _apply_costs(rec: dict, ledger: ExecutionCostLedger) -> None:
    c = ledger.costs_dict()
    rec["execution_costs"] = c
    rec["target_http_requests"] = c["target_http_requests"]
    rec["sequence_attempts"] = c["sequence_attempts"]
    rec["atheris_inputs"] = c["atheris_inputs"]
    rec["bootstrap_http_requests"] = c["bootstrap_http_requests"]
    rec["checkpoint_validation_http_requests"] = c[
        "checkpoint_validation_http_requests"]
    rec["prefix_replay_http_requests"] = c["prefix_replay_http_requests"]
    rec["suffix_http_requests"] = c["suffix_http_requests"]
    rec["observer_calls"] = c["observer_calls"]
    rec["reset_calls"] = c["reset_calls"]
    rec["scheduler_decisions"] = c["scheduler_decisions"]
    rec["elapsed_wall_seconds"] = c["elapsed_wall_seconds"]
    rec["analysis_wall_seconds"] = c["analysis_wall_seconds"]
    rec["bootstrap_execs"] = c["bootstrap_http_requests"]
    rec["atheris_execs"] = c["atheris_inputs"]
    rec["checkpoint_validation_execs"] = c[
        "checkpoint_validation_http_requests"]
    rec["prefix_replay_execs"] = c["prefix_replay_http_requests"]
    rec["total_target_execs"] = c["target_http_requests"]
    rec["http_requests_executed"] = c["target_http_requests"]
    rec["sequence_evaluations"] = c["sequence_attempts"]


def run_protocol(seeds=(1, 2, 3), seconds: float = 1.0,
                 total_target_exec_budget: int = 120, max_steps: int = 3,
                 arms=ARMS, out_root: Path | None = None) -> dict:
    out_root = Path(out_root or (ROOT / "artifacts" / "session_ss06"))
    raw_dir = out_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    analysis_started = time.monotonic()
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
    analysis_seconds = round(time.monotonic() - analysis_started, 6)
    bootstrap_budget = max(1, min(60, total_target_exec_budget // 2))
    manifest = {
        "protocol": PROTOCOL,
        "command": sys.argv,
        "source_revision": _revision(),
        "versions": _versions(),
        "target_block": TARGET_BLOCK,
        "writer_block": WRITER_BLOCK,
        "seeds": list(seeds),
        "seconds": seconds,
        "total_target_exec_budget": total_target_exec_budget,
        "bootstrap_budget": bootstrap_budget,
        "max_steps": max_steps,
        "arms": list(arms),
        "request_pool": POOL,
        "budget_boundary": "target-callable-invocation (one HTTP request)",
        "analysis_wall_seconds": analysis_seconds,
        "notes": ("target_http_requests is the hard work budget and is "
                  "recomputed from per_request.jsonl. Atheris input count is "
                  "reported separately. Bootstrap, checkpoint validation, "
                  "prefix replays and isolation controls all pass through the "
                  "same ledger. STATE_REUSE does not call bootstrap; it uses "
                  "the frozen C preparation checkpoint and reports inherited "
                  "C cost separately from its own replay cost."),
    }
    (out_root / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")

    runs: list[dict] = []
    for seed in seeds:
        c_ctx: dict | None = None
        for arm in arms:
            arm_started = time.monotonic()
            corpus = raw_dir / f"seed{seed}" / arm / "corpus"
            _seed_corpus(corpus)
            out = raw_dir / f"seed{seed}" / arm / "out"
            out.mkdir(parents=True, exist_ok=True)
            (out / "ir.json").write_bytes(
                (out_root / "program_ir.json").read_bytes())
            ledger = ExecutionCostLedger(
                out_dir=out / "ledger", run_id=f"{arm}-{seed}", arm=arm,
                seed=seed, target_block=TARGET_BLOCK, app=str(GATE),
                request_limit=total_target_exec_budget)

            if arm == "SINGLE":
                rec = _run_atheris(seed, arm, corpus, out, seconds, pool_path,
                                   max_runs=total_target_exec_budget,
                                   max_requests=total_target_exec_budget,
                                   run_id=f"{arm}-{seed}")
                ledger.merge(rec["execution_costs"])
                ledger.override_elapsed(rec["elapsed_wall_seconds"])
                ledger.finish()
                _apply_costs(rec, ledger)
                rec["analysis_wall_seconds"] = analysis_seconds
                runs.append(rec)
                continue

            if arm == "STATE_REUSE":
                rec = _base_record(seed, arm)
                if c_ctx is None or c_ctx["prep"] is None:
                    rec["failures"].append("NO_CHECKPOINT")
                    rec["elapsed_wall_seconds"] = round(
                        time.monotonic() - arm_started, 6)
                    runs.append(rec)
                    continue
                c_prep = c_ctx["prep"]
                c_mod = c_ctx["mod"]
                c_record = c_ctx["record"]
                truthy_group = query_session_frontier(
                    ir, index, TRUTHY_BLOCK, accesses).path_constraint_groups
                truthy_ckpt = dataclasses.replace(
                    c_prep, target_block=TRUTHY_BLOCK,
                    condition_groups=[[c.to_dict() for c in g]
                                      for g in truthy_group[:1]])
                primary = replay_session_checkpoint(
                    c_prep, c_mod.target, ir,
                    reset_hook=c_mod.reset_state,
                    session_probe=c_mod.session_view,
                    trace_root=GATE,
                    suffix_steps=[{"method": "GET", "path": "/protected"}],
                    cost_ledger=ledger)
                truthy = replay_session_checkpoint(
                    truthy_ckpt, c_mod.target, ir,
                    reset_hook=c_mod.reset_state,
                    session_probe=c_mod.session_view,
                    trace_root=GATE,
                    suffix_steps=[{"method": "GET",
                                   "path": "/protected_truthy"}],
                    cost_ledger=ledger)
                control_target = ledger.wrap_target(c_mod.target,
                                                    phase="suffix")
                ledger.begin_sequence("isolation-control")
                try:
                    isolation = not suffix_without_prefix_reaches_target(
                        c_prep, control_target, ir,
                        reset_hook=ledger.wrap_reset(c_mod.reset_state),
                        trace_root=GATE,
                        suffix_steps=[{"method": "GET",
                                       "path": "/protected"}])
                finally:
                    ledger.end_sequence()
                ledger.finish()
                _apply_costs(rec, ledger)
                rec["analysis_wall_seconds"] = analysis_seconds
                rec["target_reached"] = bool(primary.suffix_target_covered)
                rec["session_condition_verified"] = bool(
                    primary.preparation_verified
                    and truthy.preparation_verified)
                rec["preparation_verified"] = primary.preparation_verified
                rec["suffix_target_covered"] = primary.suffix_target_covered
                rec["truthy_suffix_target_covered"] = \
                    truthy.suffix_target_covered
                rec["suffix_new_blocks"] = sorted(
                    set(primary.suffix_incremental_blocks)
                    | set(truthy.suffix_incremental_blocks))
                rec["prefix_length"] = c_prep.prefix_len
                rec["checkpoint_kind"] = c_prep.checkpoint_kind
                rec["checkpoint_id"] = c_prep.checkpoint_id
                rec["suffix_without_prefix_isolated"] = isolation
                rec["replay_status"] = primary.status
                rec["truthy_replay_status"] = truthy.status
                rec["checkpoint_source_arm"] = "STATE_GUIDED"
                rec["shared_preprocessing_costs"] = c_record[
                    "execution_costs"]
                end_to_end = ExecutionCosts()
                end_to_end.add(c_record["execution_costs"])
                end_to_end.add(rec["execution_costs"])
                rec["end_to_end_costs"] = end_to_end.to_dict()
                rec["end_to_end_target_http_requests"] = end_to_end.to_dict()[
                    "target_http_requests"]
                rec["end_to_end_elapsed_wall_seconds"] = round(
                    c_record["elapsed_wall_seconds"]
                    + rec["elapsed_wall_seconds"], 6)
                runs.append(rec)
                continue

            # SEQUENCE / STATE_GUIDED: bootstrap then Atheris.
            search_mod, search_result = _bootstrap(
                arm, seed, ir, frontier, out, corpus,
                max_execs=bootstrap_budget, max_steps=max_steps,
                ledger=ledger)
            rec = _base_record(seed, arm)
            rec.update({
                "bootstrap_status": search_result.status,
                "bootstrap_found": search_result.found,
                "bootstrap_steps": [dict(s) for s in search_result.steps],
                "sequence_length": len(search_result.steps) or None,
                "guided": search_result.guided,
            })
            if arm == "STATE_GUIDED":
                c_ctx = {"mod": search_mod, "prep": None, "record": None}
                prep = _prep_from_c(seed, ir, frontier, search_result,
                                    search_mod, ledger)
                if prep is not None:
                    rec["prefix_length"] = prep.prefix_len
                    rec["checkpoint_kind"] = prep.checkpoint_kind
                    rec["checkpoint_id"] = prep.checkpoint_id
                    validation = replay_session_checkpoint(
                        prep, search_mod.target, ir,
                        reset_hook=search_mod.reset_state,
                        session_probe=search_mod.session_view,
                        trace_root=GATE, suffix_steps=[],
                        cost_ledger=ledger)
                    rec["checkpoint_validation_verified"] = bool(
                        validation.preparation_verified)
                    c_ctx["prep"] = (prep if validation.preparation_verified
                                     else None)
                c_ctx["record"] = rec

            remaining = max(1, total_target_exec_budget
                            - ledger.costs.target_http_requests)
            atheris = _run_atheris(
                seed, arm, corpus, out, seconds, pool_path,
                max_runs=remaining, max_requests=remaining,
                run_id=f"{arm}-{seed}")
            parent_elapsed = ledger.costs.elapsed_wall_seconds
            ledger.merge(atheris["execution_costs"])
            ledger.override_elapsed(
                parent_elapsed + atheris["elapsed_wall_seconds"])
            ledger.finish()
            _apply_costs(rec, ledger)
            rec["analysis_wall_seconds"] = analysis_seconds
            rec.update({
                "target_reached": atheris["target_reached"],
                "writer_covered": atheris.get("writer_covered"),
                "session_condition_verified": atheris[
                    "session_condition_verified"],
                "verified_atoms": atheris.get("verified_atoms"),
                "proxy_atoms": atheris.get("proxy_atoms"),
                "guided_status": atheris.get("guided_status"),
                "guided_found": atheris.get("guided_found"),
                "corpus_sequences": atheris.get("corpus_sequences"),
                "failures": atheris.get("failures", []),
            })
            if arm == "STATE_GUIDED" and c_ctx is not None:
                c_ctx["record"] = rec
            runs.append(rec)

    summary = _summarize(runs, manifest)
    (out_root / "per_run.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in runs) + "\n",
        encoding="utf-8")
    (out_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    report = _render(summary)
    (out_root / "SESSION_SS06_REPORT.md").write_text(report,
                                                     encoding="utf-8")
    (out_root / "REPORT.md").write_text(report, encoding="utf-8")
    # Root-level append-only logs make the whole run recomputable without
    # knowing the arm directory layout.  Child Atheris ledgers and parent
    # search/replay ledgers are concatenated once.
    for name in ("per_request.jsonl", "per_sequence.jsonl",
                 "scheduler.jsonl"):
        with (out_root / name).open("w", encoding="utf-8") as dst:
            for seed in seeds:
                for arm in arms:
                    for sub in ("ledger", "atheris_cost"):
                        src = (raw_dir / f"seed{seed}" / arm / "out" /
                               sub / name)
                        if src.exists():
                            dst.write(src.read_text(encoding="utf-8"))
    return {"manifest": manifest, "runs": runs, "summary": summary}


def _summarize(runs: list[dict], manifest: dict) -> dict:
    def mean(rows, key):
        return round(sum(r.get(key, 0) or 0 for r in rows) / len(rows), 3) \
            if rows else 0.0

    arms = {}
    for arm in ARMS:
        subset = [r for r in runs if r["arm"] == arm]
        if not subset:
            continue
        arms[arm] = {
            "runs": len(subset),
            "target_reached": sum(1 for r in subset if r["target_reached"]),
            "session_condition_verified": sum(
                1 for r in subset if r["session_condition_verified"]),
            "mean_target_http_requests": mean(subset,
                                              "target_http_requests"),
            "mean_atheris_inputs": mean(subset, "atheris_inputs"),
            "mean_sequence_attempts": mean(subset, "sequence_attempts"),
            "mean_bootstrap_http_requests": mean(
                subset, "bootstrap_http_requests"),
            "mean_checkpoint_validation_http_requests": mean(
                subset, "checkpoint_validation_http_requests"),
            "mean_prefix_replay_http_requests": mean(
                subset, "prefix_replay_http_requests"),
            "mean_suffix_http_requests": mean(subset,
                                              "suffix_http_requests"),
            "mean_observer_calls": mean(subset, "observer_calls"),
            "mean_reset_calls": mean(subset, "reset_calls"),
            "mean_elapsed_wall_seconds": mean(subset,
                                              "elapsed_wall_seconds"),
            "mean_end_to_end_target_http_requests": mean(
                subset, "end_to_end_target_http_requests"),
            "mean_end_to_end_wall_seconds": mean(
                subset, "end_to_end_elapsed_wall_seconds"),
            "failures": [r["failures"] for r in subset if r["failures"]],
        }
    paired = []
    for seed in manifest["seeds"]:
        def get(arm):
            return next((r for r in runs
                         if r["seed"] == seed and r["arm"] == arm), None)
        b, c, d = get("SEQUENCE"), get("STATE_GUIDED"), get("STATE_REUSE")
        if b and c:
            paired.append({
                "seed": seed,
                "C_minus_B_target_http_requests":
                    c["target_http_requests"] - b["target_http_requests"],
                "C_target_http_requests": c["target_http_requests"],
                "B_target_http_requests": b["target_http_requests"],
                "B_target": b["target_reached"],
                "C_target": c["target_reached"],
                "B_verified": b["session_condition_verified"],
                "C_verified": c["session_condition_verified"],
                "D_actual_target_http_requests":
                    d["target_http_requests"] if d else None,
                "D_end_to_end_target_http_requests":
                    d.get("end_to_end_target_http_requests") if d else None,
                "D_target": d["target_reached"] if d else None,
            })
    return {
        "protocol": PROTOCOL,
        "source_revision": manifest["source_revision"],
        "seeds": manifest["seeds"],
        "total_target_exec_budget": manifest["total_target_exec_budget"],
        "analysis_wall_seconds": manifest.get("analysis_wall_seconds", 0.0),
        "arms": arms,
        "paired_C_minus_B": paired,
    }


def _render(s: dict) -> str:
    lines = [
        "# SS-07A Session 四臂实验（物理请求计费）", "",
        f"- protocol: `{s['protocol']}`",
        f"- commit: `{s['source_revision']}`",
        f"- seeds: `{s['seeds']}`",
        f"- 真实请求预算: `{s.get('total_target_exec_budget')}`",
        f"- 静态分析墙钟: `{s.get('analysis_wall_seconds')}` 秒",
        "- 目标：`app.py::protected::173-173::block::1`",
        "- 每个 target 调用 = 1 个真实 HTTP 请求；多步输入 = 1 个 sequence、"
        "多个请求。bootstrap、验证、前缀重放、隔离对照全部计费。",
        "",
        "## 汇总", "",
        "| arm | target | verified | HTTP | Atheris inputs | bootstrap | validation | prefix replay | suffix | wall(s) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for arm, a in s["arms"].items():
        lines.append(
            f"| {arm} | {a['target_reached']}/{a['runs']} | "
            f"{a['session_condition_verified']}/{a['runs']} | "
            f"{a['mean_target_http_requests']} | "
            f"{a['mean_atheris_inputs']} | "
            f"{a['mean_bootstrap_http_requests']} | "
            f"{a['mean_checkpoint_validation_http_requests']} | "
            f"{a['mean_prefix_replay_http_requests']} | "
            f"{a['mean_suffix_http_requests']} | "
            f"{a['mean_elapsed_wall_seconds']} |")
    lines += ["", "## C vs B 配对（真实请求）", ""]
    if s["paired_C_minus_B"]:
        lines += ["| seed | B HTTP | C HTTP | C-B | B target | C target | D actual HTTP | D end-to-end HTTP |",
                  "|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for p in s["paired_C_minus_B"]:
            lines.append(
                f"| {p['seed']} | {p['B_target_http_requests']} | "
                f"{p['C_target_http_requests']} | "
                f"{p['C_minus_B_target_http_requests']} | "
                f"{p['B_target']} | {p['C_target']} | "
                f"{p['D_actual_target_http_requests']} | "
                f"{p['D_end_to_end_target_http_requests']} |")
    else:
        lines.append("没有同时运行 B 和 C 的配对结果。")
    lines += ["", "## 诚实边界", "",
              "- 这是小样本功能对照，不宣称统计显著。",
              "- Atheris 输入数与真实 HTTP 请求数分开报告；"
              "Atheris 初始化可能多执行输入，原始日志保留。",
              "- D 的实际 HTTP 只计它自己的重放；"
              "`end_to_end` 才包含 C 的前缀发现/验证成本，二者不混用。",
              "- D 的 `bootstrap_http_requests` 必须为 0；若没有可信 C 前缀，"
              "记录 `NO_CHECKPOINT`，不回退到完整成功链。",
              "- 前缀复用是请求重放，不是物理内存快照。",
              "", "原始记录：`per_run.jsonl`、各 arm 的 `ledger/per_request.jsonl`、`run_manifest.json`。"]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", default="1,2,3")
    parser.add_argument("--seconds", type=float, default=1.0)
    parser.add_argument("--total-target-exec-budget", type=int, default=120)
    parser.add_argument("--max-steps", type=int, default=3)
    parser.add_argument("--arms", default=",".join(ARMS))
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    seeds = tuple(int(x) for x in args.seeds.split(",") if x.strip())
    arms = tuple(x.strip() for x in args.arms.split(",") if x.strip())
    unknown = set(arms) - set(ARMS)
    if unknown:
        parser.error(f"unknown arms: {sorted(unknown)}")
    run_protocol(seeds=seeds, seconds=args.seconds,
                 total_target_exec_budget=args.total_target_exec_budget,
                 max_steps=args.max_steps, arms=arms,
                 out_root=Path(args.out) if args.out else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
