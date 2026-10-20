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
               max_execs: int, max_steps: int):
    mod = _load_target(f"ss06_{arm}_{seed}")
    guided = arm == "STATE_GUIDED"
    result = SessionGuidedSearch(
        mod.target, ir, TARGET_BLOCK, frontier, POOL,
        max_execs=max_execs, max_steps=max_steps, seed=seed, guided=guided,
        reset_hook=mod.reset_state, trace_root=GATE,
        session_probe=_probe(mod)).run()
    if result.seed:
        cid = __import__("hashlib").sha256(result.seed).hexdigest()[:16]
        (corpus / cid).write_bytes(result.seed)
    return mod, result


def _prep_from_c(seed: int, ir, frontier, result, mod):
    return preparation_checkpoint_from_guided_result(
        result, GATE / "app.py", ir, target=mod.target,
        reset_hook=mod.reset_state, session_probe=mod.session_view,
        trace_root=GATE)


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
        "elapsed_wall_seconds": 0.0,
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
                 pool_path: Path, max_runs: int) -> dict:
    t0 = time.monotonic()
    raw = _run_driver(arm, seed, corpus, out, seconds, pool_path,
                      guided=False, max_runs=max_runs)
    rec = _base_record(seed, arm)
    rec.update({
        "atheris_execs": raw["atheris_executions"],
        "total_target_execs": raw["atheris_executions"],
        "sequence_evaluations": raw["atheris_executions"],
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
        "http_requests_executed": sum(
            s["length"] for s in raw["corpus_sequences"]),
        "failures": [] if raw["returncode"] == 0 else [
            f"driver-returncode-{raw['returncode']}",
            raw.get("stderr_tail", "")],
        "stop_reason": raw.get("stop_reason"),
    })
    return rec


def run_protocol(seeds=(1, 2, 3), seconds: float = 1.0,
                 total_target_exec_budget: int = 120, max_steps: int = 3,
                 arms=ARMS, out_root: Path | None = None) -> dict:
    out_root = Path(out_root or (ROOT / "artifacts" / "session_ss06"))
    raw_dir = out_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
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
        "notes": ("bootstrap, checkpoint validation and prefix replay are "
                  "counted as target executions; Atheris counts inputs, not "
                  "HTTP requests. http_requests_executed is derived from "
                  "corpus sequence lengths, so it is a lower-bound accounting "
                  "aid, not a per-input counter. Atheris may execute corpus "
                  "seeds while initializing, so a small overrun above "
                  "total_target_exec_budget is recorded in the raw rows and "
                  "must not be hidden."),
    }
    (out_root / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")

    runs: list[dict] = []
    for seed in seeds:
        c_record = None
        c_prep = None
        c_mod = None
        for arm in arms:
            corpus = raw_dir / f"seed{seed}" / arm / "corpus"
            _seed_corpus(corpus)
            out = raw_dir / f"seed{seed}" / arm / "out"
            out.mkdir(parents=True, exist_ok=True)
            (out / "ir.json").write_bytes(
                (out_root / "program_ir.json").read_bytes())

            if arm == "SINGLE":
                rec = _run_atheris(seed, arm, corpus, out, seconds, pool_path,
                                   max_runs=total_target_exec_budget)
                runs.append(rec)
                continue

            search_mod, search_result = _bootstrap(
                arm, seed, ir, frontier, out, corpus,
                max_execs=bootstrap_budget, max_steps=max_steps)
            rec = _base_record(seed, arm)
            rec.update({
                "bootstrap_execs": search_result.executions,
                "bootstrap_status": search_result.status,
                "bootstrap_found": search_result.found,
                "bootstrap_steps": [dict(s) for s in search_result.steps],
                "sequence_length": len(search_result.steps) or None,
                "stop_reason": search_result.stop_reason,
                "guided": search_result.guided,
            })
            if arm == "STATE_GUIDED":
                c_record = rec
                c_mod = search_mod
                c_prep = _prep_from_c(seed, ir, frontier, search_result,
                                      search_mod)
                if c_prep is not None:
                    rec["prefix_length"] = c_prep.prefix_len
                    rec["checkpoint_kind"] = c_prep.checkpoint_kind
                    rec["checkpoint_id"] = c_prep.checkpoint_id
                    rec["checkpoint_validation_execs"] = c_prep.prefix_len

            if arm == "STATE_REUSE":
                if c_prep is None or c_mod is None or c_record is None:
                    rec["failures"].append("no-preparation-checkpoint")
                    runs.append(rec)
                    continue
                # Two suffixes, each from a cold reset + replayed prefix.
                truthy_group = query_session_frontier(
                    ir, index, TRUTHY_BLOCK, accesses).path_constraint_groups
                truthy_ckpt = dataclasses.replace(
                    c_prep, target_block=TRUTHY_BLOCK,
                    condition_groups=[[c.to_dict() for c in g]
                                      for g in truthy_group[:1]])
                primary = replay_session_checkpoint(
                    c_prep, c_mod.target, ir, reset_hook=c_mod.reset_state,
                    session_probe=c_mod.session_view, trace_root=GATE,
                    suffix_steps=[{"method": "GET", "path": "/protected"}])
                truthy = replay_session_checkpoint(
                    truthy_ckpt, c_mod.target, ir,
                    reset_hook=c_mod.reset_state,
                    session_probe=c_mod.session_view, trace_root=GATE,
                    suffix_steps=[{"method": "GET",
                                   "path": "/protected_truthy"}])
                isolation = not suffix_without_prefix_reaches_target(
                    c_prep, c_mod.target, ir, reset_hook=c_mod.reset_state,
                    trace_root=GATE,
                    suffix_steps=[{"method": "GET", "path": "/protected"}])
                prefix_runs = len(c_prep.prefix_steps()) * 2
                suffix_runs = 2
                rec.update({
                    "bootstrap_execs": c_record["bootstrap_execs"],
                    "bootstrap_status": c_record.get("bootstrap_status"),
                    "bootstrap_found": c_record.get("bootstrap_found"),
                    "bootstrap_steps": c_record.get("bootstrap_steps"),
                    "checkpoint_validation_execs":
                        c_record.get("checkpoint_validation_execs", 0),
                    "prefix_replay_execs": prefix_runs,
                    "http_requests_executed": prefix_runs + suffix_runs,
                    "sequence_evaluations": 2,
                    "total_target_execs": (
                        c_record["bootstrap_execs"]
                        + c_record.get("checkpoint_validation_execs", 0)
                        + prefix_runs + suffix_runs),
                    "elapsed_wall_seconds": 0.0,
                    "target_reached": bool(primary.suffix_target_covered),
                    "session_condition_verified": bool(
                        primary.preparation_verified
                        and truthy.preparation_verified),
                    "preparation_verified": primary.preparation_verified,
                    "suffix_target_covered": primary.suffix_target_covered,
                    "truthy_suffix_target_covered":
                        truthy.suffix_target_covered,
                    "suffix_new_blocks": sorted(
                        set(primary.suffix_incremental_blocks)
                        | set(truthy.suffix_incremental_blocks)),
                    "prefix_length": c_prep.prefix_len,
                    "checkpoint_kind": c_prep.checkpoint_kind,
                    "checkpoint_id": c_prep.checkpoint_id,
                    "suffix_without_prefix_isolated": isolation,
                    "replay_status": primary.status,
                    "truthy_replay_status": truthy.status,
                })
                runs.append(rec)
                continue

            # SEQUENCE / STATE_GUIDED: run Atheris after bootstrap, with the
            # remaining shared budget.
            remaining = max(1, total_target_exec_budget
                            - search_result.executions)
            atheris = _run_atheris(seed, arm, corpus, out, seconds,
                                   pool_path, max_runs=remaining)
            rec.update({
                "atheris_execs": atheris["atheris_execs"],
                "total_target_execs": search_result.executions
                + int(rec.get("checkpoint_validation_execs", 0) or 0)
                + atheris["atheris_execs"],
                "http_requests_executed": atheris["http_requests_executed"],
                "elapsed_wall_seconds": atheris["elapsed_wall_seconds"],
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
            runs.append(rec)

    summary = _summarize(runs, manifest)
    (out_root / "per_run.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in runs) + "\n",
        encoding="utf-8")
    (out_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_root / "SESSION_SS06_REPORT.md").write_text(
        _render(summary), encoding="utf-8")
    return {"manifest": manifest, "runs": runs, "summary": summary}


def _summarize(runs: list[dict], manifest: dict) -> dict:
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
            "mean_total_target_execs": round(
                sum(r["total_target_execs"] for r in subset) / len(subset), 2),
            "mean_atheris_execs": round(
                sum(r["atheris_execs"] for r in subset) / len(subset), 2),
            "mean_bootstrap_execs": round(
                sum(r["bootstrap_execs"] for r in subset) / len(subset), 2),
            "mean_checkpoint_validation_execs": round(
                sum(r["checkpoint_validation_execs"] for r in subset)
                / len(subset), 2),
            "mean_prefix_replay_execs": round(
                sum(r["prefix_replay_execs"] for r in subset) / len(subset), 2),
            "mean_elapsed_wall_seconds": round(
                sum(r["elapsed_wall_seconds"] for r in subset) / len(subset), 3),
            "failures": [r["failures"] for r in subset if r["failures"]],
        }
    paired = []
    for seed in manifest["seeds"]:
        def get(arm):
            return next((r for r in runs
                         if r["seed"] == seed and r["arm"] == arm), None)
        b, c = get("SEQUENCE"), get("STATE_GUIDED")
        if b and c:
            paired.append({
                "seed": seed,
                "C_minus_B_total_execs":
                    c["total_target_execs"] - b["total_target_execs"],
                "B_target": b["target_reached"],
                "C_target": c["target_reached"],
                "B_verified": b["session_condition_verified"],
                "C_verified": c["session_condition_verified"],
            })
    return {
        "protocol": PROTOCOL,
        "source_revision": manifest["source_revision"],
        "seeds": manifest["seeds"],
        "arms": arms,
        "paired_C_minus_B": paired,
    }


def _render(s: dict) -> str:
    lines = [
        "# SS-06 Session 四臂消融实验", "",
        f"- protocol: `{s['protocol']}`",
        f"- commit: `{s['source_revision']}`",
        f"- seeds: `{s['seeds']}`",
        "- 目标：`app.py::protected::173-173::block::1`",
        "- 预算、靶、请求池、最大步数、reset 规则与随机种子在各臂一致；"
        "bootstrap、检查点验证、前缀重放都计入。",
        "",
        "## 汇总", "",
        "| arm | target | verified | mean total | mean Atheris | mean bootstrap | mean validation | mean prefix replay | mean wall(s) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for arm, a in s["arms"].items():
        lines.append(
            f"| {arm} | {a['target_reached']}/{a['runs']} | "
            f"{a['session_condition_verified']}/{a['runs']} | "
            f"{a['mean_total_target_execs']} | {a['mean_atheris_execs']} | "
            f"{a['mean_bootstrap_execs']} | "
            f"{a['mean_checkpoint_validation_execs']} | "
            f"{a['mean_prefix_replay_execs']} | "
            f"{a['mean_elapsed_wall_seconds']} |")
    lines += ["", "## C vs B 配对", ""]
    if s["paired_C_minus_B"]:
        lines += ["| seed | C-B total execs | B target | C target | B verified | C verified |",
                  "|---:|---:|---:|---:|---:|---:|"]
        for p in s["paired_C_minus_B"]:
            lines.append(
                f"| {p['seed']} | {p['C_minus_B_total_execs']} | "
                f"{p['B_target']} | {p['C_target']} | "
                f"{p['B_verified']} | {p['C_verified']} |")
    else:
        lines.append("没有同时运行 B 和 C 的配对结果。")
    lines += ["", "## 诚实边界", "",
              "- 这是一个小样本功能对照，不宣称统计显著。",
              "- Atheris 统计的是输入次数，不是 HTTP 请求数；"
              "`http_requests_executed` 只能由语料长度做下界估计。",
              "- STATE_REUSE 的前缀是请求计划，不是物理内存快照；"
              "每个后缀仍会重新执行前缀，成本已计入。",
              "- STATE_REUSE 的 total 是状态准备前缀的发现/验证 + 两个后缀的成本，"
              "不包含 STATE_GUIDED 中为目标单独跑的 Atheris；它回答的是"
              "“准备前缀能否复用”，不是“D 比 C 快”。",
              "- 如果 C 没有优于 B，报告不推断“静态图无价值”，"
              "也可能只是靶太简单。",
              "", "原始记录：`per_run.jsonl`、`run_manifest.json`、`raw/`。"]
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
