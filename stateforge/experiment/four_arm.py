"""Phase-4.2-G 修正版:时间预算对齐 + 大语义靶 + request 接口 LLM 臂。

修复:
① Am 跑 seconds×epochs 连续(与 B/C/D 同总时长)
② 靶用 S1(70 块)/S2(73 块)/hash_gates(对照)
③ D 臂直接在 request 版靶上跑 LLMCoverageLoop(不经过 bytes wrapper)
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

from stateforge.experiment.epoch import run_epochs


def _llm_solving(app_path: Path, seed: dict, workdir: Path,
                 mock_responses: list[str]) -> dict:
    from stateforge.feasibility.coverage_loop import LLMCoverageLoop
    from stateforge.llm.real_provider import MockLLMProvider
    provider = MockLLMProvider(mock_responses)
    loop = LLMCoverageLoop(app_path=app_path, seed=seed,
                           provider=provider, workdir=workdir,
                           max_rounds=6, max_attempts_per_frontier=3)
    result = loop.run()
    return {"target_reached": result.target_reached,
            "crossed": sum(1 for a in result.attempts if a.frontier_crossed),
            "gain_blocks": sum(a.new_blocks for a in result.attempts),
            "attempts": len(result.attempts),
            "records": result.attempt_records}


# 每靶配置:bytes 靶(A/B/C 用)、request 靶(D 用)、LLM mock、种子
TARGETS = [
    {
        "name": "S1_role_chain",
        "bytes_target": "stateforge/targets/microplus/s1_role_chain.py",
        "request_target": "stateforge/fixtures/llm_loop/S1_role_chain/app.py",
        "ir_bytes": "/tmp/ir_all",  # 统一 IR(所有 microplus 靶)
        "ir_request": "/tmp/ir_S1",
        "seed": {"query": {"username": "bob", "action": "view", "token": "x"}},
        "llm_mock": [
            json.dumps({"status": "PROPOSED", "source_kind": "QUERY",
                        "source_key": "username", "operation": "REPLACE",
                        "proposed_value": "administrator",
                        "rationale": "admin role", "confidence": 0.9}),
            json.dumps({"status": "PROPOSED", "source_kind": "QUERY",
                        "source_key": "action", "operation": "REPLACE",
                        "proposed_value": "delete",
                        "rationale": "priv action", "confidence": 0.9}),
            json.dumps({"status": "PROPOSED", "source_kind": "QUERY",
                        "source_key": "token", "operation": "REPLACE",
                        "proposed_value": "adm_secret",
                        "rationale": "admin prefix", "confidence": 0.9}),
        ],
    },
    {
        "name": "S2_api_path",
        "bytes_target": "stateforge/targets/microplus/s2_api_path.py",
        "request_target": "stateforge/fixtures/llm_loop/S2_api_path/app.py",
        "ir_bytes": "/tmp/ir_all",
        "ir_request": "/tmp/ir_S2",
        "seed": {"query": {"path": "/", "method": "GET", "version": "v0"}},
        "llm_mock": [
            json.dumps({"status": "PROPOSED", "source_kind": "QUERY",
                        "source_key": "version", "operation": "REPLACE",
                        "proposed_value": "v1",
                        "rationale": "v1 routing", "confidence": 0.9}),
            json.dumps({"status": "PROPOSED", "source_kind": "QUERY",
                        "source_key": "path", "operation": "REPLACE",
                        "proposed_value": "/api/v1/admin/config",
                        "rationale": "admin endpoint", "confidence": 0.9}),
            json.dumps({"status": "PROPOSED", "source_kind": "QUERY",
                        "source_key": "method", "operation": "REPLACE",
                        "proposed_value": "GET",
                        "rationale": "read config", "confidence": 0.9}),
        ],
    },
    {
        "name": "hash_gates",
        "bytes_target": "stateforge/targets/microplus/hash_gates.py",
        "request_target": None,      # hash_gates 无 request 版
        "ir_bytes": "/tmp/ir_all",
        "ir_request": None,
        "seed": {"query": {"x": "1"}},
        "llm_mock": [],              # 哈希门 LLM 解不了(诚实)
    },
]


def run_fixed(target_cfg: dict, out: Path, runs: int, epochs: int,
              seconds: int) -> dict:
    name = target_cfg["name"]
    bt = target_cfg["bytes_target"]
    ir_b = str(Path(target_cfg["ir_bytes"]) / "program_ir.json")
    total_time = epochs * seconds
    results = []

    for k in range(runs):
        seed = 7 + k * 100
        rd = out / f"run{k:02d}"
        print(f"  [{name}] run {k+1}/{runs}", flush=True)

        # Am:连续跑 total_time(时间预算对齐!)
        am = run_epochs(bt, ir_b, rd / "Am", total_time, 1, seed,
                        "Am", mode="measure", use_feedback=False)
        # B:重启+结构反馈
        b = run_epochs(bt, ir_b, rd / "B", seconds, epochs, seed,
                        "B", mode="feedback", use_feedback=True)
        # C:重启+前沿定向
        c = run_epochs(bt, ir_b, rd / "C", seconds, epochs, seed,
                        "C", mode="frontier", use_feedback=True,
                        policy="portfolio")

        # D:C 的覆盖 + LLM 求解(在 request 靶上)
        llm_res = {"crossed": 0, "gain_blocks": 0, "target_reached": False}
        if target_cfg["request_target"]:
            llm_dir = rd / "D_llm"
            llm_dir.mkdir(exist_ok=True)
            llm_res = _llm_solving(
                Path(target_cfg["request_target"]),
                target_cfg["seed"], llm_dir, target_cfg["llm_mock"])

        results.append({
            "seed": seed,
            "Am_blocks": am["final_blocks"], "Am_wall": am["wall_span"],
            "B_blocks": b["final_blocks"], "B_wall": b["wall_span"],
            "C_blocks": c["final_blocks"], "C_wall": c["wall_span"],
            "D_blocks": c["final_blocks"] + llm_res.get("gain_blocks", 0),
            "D_llm": llm_res,
        })

    summary = {"target": name, "runs": runs,
               "total_time_per_arm": total_time,
               "per_run": [{k: v for k, v in r.items() if k != "D_llm"}
                            for r in results]}
    for arm in ("Am", "B", "C", "D"):
        vals = [r[f"{arm}_blocks"] for r in results]
        summary[f"{arm}_mean"] = round(statistics.mean(vals), 1)
        summary[f"{arm}_stdev"] = round(
            statistics.stdev(vals), 2) if len(vals) > 1 else 0
    summary["D_minus_C"] = round(statistics.mean(
        [r["D_blocks"] - r["C_blocks"] for r in results]), 1)
    summary["llm_crossed_total"] = sum(
        r["D_llm"].get("crossed", 0) for r in results)
    summary["llm_target_reached"] = sum(
        1 for r in results if r["D_llm"].get("target_reached"))
    return summary


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="results/experiment/phase42g_v2")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--seconds", type=float, default=8)
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    all_s = {}

    for cfg in TARGETS:
        s = run_fixed(cfg, out / cfg["name"], args.runs,
                       args.epochs, int(args.seconds))
        all_s[cfg["name"]] = s
        print(f"  → {cfg['name']}: Am={s['Am_mean']} B={s['B_mean']} "
              f"C={s['C_mean']} D={s['D_mean']} (D-C={s['D_minus_C']}) "
              f"llm_crossed={s['llm_crossed_total']} "
              f"llm_reached={s['llm_target_reached']}/{args.runs}",
              flush=True)

    (out / "all_summaries.json").write_text(
        json.dumps(all_s, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\n[out] {out / 'all_summaries.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
