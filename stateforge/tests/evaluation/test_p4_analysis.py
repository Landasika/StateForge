"""P4-T6a:p4_analysis 聚合脚本——TDD 先行(本文件先于实现)。

夹具 = 合成 p4_formal 形状目录(camp_*/abl_*),schema 逐字段对齐两个
真实 writer 的落盘形状(campaign.py 的 summary.json per_run 行 /
state_ablation.py 的 state_ablation.json arms+state_runs+审计 JSONL),
数字手算钉死(含一个零差 null 例与一个"仅 campaign 无消融"诚实缺省例)。
绝不触碰 results/experiment/p4_formal(正式战役正写入;只读约束由
test_analysis_never_mutates_fixture 钉死)。

手算口径(与 campaign.py _stats 同款:样本 stdev,round 4):
- t_a E blocks   = [20,22,21,23,20,24,22,25,21,22]  mean=22  stdev=√(24/9)=1.633
- t_a D1 blocks  = [18,19,20,20,19,21,20,22,21,22]  mean=20.2 stdev=√(15.6/9)=1.3166
- 逐 run 差       = [2,3,1,3,1,3,2,3,0,0]           mean=1.8 median=2
                                                    stdev=√(13.6/9)=1.2293
                                                    wins/ties/losses=8/2/0
- t_a AUC 差      = [0.05,0.06,0.04,0.06,0.04,0.06,0.05,0.06,0.03,0.04]
                                                    mean=0.049 median=0.05
                                                    wins/ties/losses=10/0/0
- t_a 消融 E−E-rand blocks = [2,3,0]  mean=5/3 median=2 stdev=√(42/18)=1.5275
                                                    wins/ties/losses=2/1/0
- t_a 消融 E−E-rand atoms  = [1,1,1]  mean=1 wins=3
- t_a 消融 E−E-always      = 全 0 → null(R2A_VACUOUS:账本仅 INJECT_ALWAYS×4、
                            0 INJECTED = R2-(a) vacuous)
- t_b 战役 E−D1            = 全 0 → null(STATE_NEVER_ENGAGED:账本仅
                            SKIP_R2_PROGRESS×5、0 INJECTED)
- t_b 消融 E−E-always      = 全 0 → null(TRIGGER_NET_ZERO:INJECT_ALWAYS×3
                            且 INJECTED×3 仍逐 run 打平)
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

# ------------------------------------------------------- 夹具构造(writer 形状)

E_B = [20, 22, 21, 23, 20, 24, 22, 25, 21, 22]
D1_B = [18, 19, 20, 20, 19, 21, 20, 22, 21, 22]
E_AUC = [0.50, 0.52, 0.51, 0.53, 0.50, 0.54, 0.52, 0.55, 0.51, 0.52]
D1_AUC = [0.45, 0.46, 0.47, 0.47, 0.46, 0.48, 0.47, 0.49, 0.48, 0.48]
E_ATOMS_CT = [1, 1, 1, 1, 1, 1, 1, 1, 2, 2]
E_INJ = [5, 5, 4, 5, 5, 6, 5, 5, 4, 5]


def _camp_row(seed, d1_b, d1_auc, e_b, e_auc, e_atoms=None, e_inj=None):
    """campaign.py per_run 行形状(E/D1 臂;state 字段按设计可缺)。"""

    def _arm(b, auc):
        return {"total_executions": 100, "wall_span": 30.0, "final_blocks": b,
                "final_edges": 40, "total_feedback_seconds": 1.0,
                "throughput": 3.3, "auc_blocks_abs": 600.0,
                "auc_blocks_norm": auc, "auc_edges_abs": 120.0,
                "auc_edges_norm": 0.3}

    e = _arm(e_b, e_auc)
    if e_atoms is not None:
        e["state_atoms_reached"] = e_atoms
    if e_inj is not None:
        e["state_injections_total"] = e_inj
    return {"seed": seed, "D1": _arm(d1_b, d1_auc), "E": e}


def _write_camp(root, target, rows, run_atoms=None, ledgers=None):
    d = root / f"camp_{target}"
    d.mkdir(parents=True)
    summary = {"manifest": {"target": f"{target}.py", "runs": len(rows)},
               "per_run": rows, "aggregate": {}, "questions": {}}
    (d / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    for k, atoms in (run_atoms or {}).items():
        rd = d / f"run{k:02d}" / "E"
        rd.mkdir(parents=True)
        (rd / "group_summary.json").write_text(
            json.dumps({"group": "E", "state_atoms": atoms}), encoding="utf-8")
    for k, recs in (ledgers or {}).items():
        rd = d / f"run{k:02d}" / "E" / "epochs"
        rd.mkdir(parents=True)
        (rd / "state_injections.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs),
            encoding="utf-8")
    return d


def _write_abl(root, target, arms_blocks, arms_atoms, arms_inj, ledgers=None):
    """state_ablation.py state_ablation.json 形状(arms+state_runs;pairwise
    留空——分析脚本按 per_run 自行配对,不消费 writer 的聚合)。"""
    d = root / f"abl_{target}"
    d.mkdir(parents=True)
    runs = len(next(iter(arms_blocks.values())))
    arms = {}
    for arm, blocks in arms_blocks.items():
        per_run, state_runs = {}, []
        for k in range(runs):
            run = f"run{k:02d}"
            atoms = list(arms_atoms[arm][k])
            per_run[run] = {"seed": 7 + k * 100, "blocks": blocks[k],
                            "edges": 40, "executions": 100,
                            "injections_total": arms_inj[arm][k],
                            "atoms_reached": len(atoms),
                            "transitions_observed": len(atoms),
                            "deep_state_blocks": 0}
            state_runs.append({"run": run, "atoms_reached": len(atoms),
                               "atoms": atoms,
                               "transitions_observed": len(atoms),
                               "transitions": [], "deep_state_blocks": 0,
                               "deep_block_ids": [], "deep_functions": []})
        union = sorted({a for r in state_runs for a in r["atoms"]})
        arms[arm] = {"per_run": per_run, "state_runs": state_runs,
                     "state": {"atoms_reached": len(union), "atoms": union,
                               "transitions_observed": 0, "transitions": [],
                               "deep_state_blocks": 0, "deep_block_ids": [],
                               "deep_functions": []},
                     "mean_blocks": round(sum(blocks) / runs, 1),
                     "mean_edges": 40.0}
    rep = {"target": f"{target}.py", "ir": "ir.json", "runs": runs,
           "epochs": 3, "seconds": 3.0, "seed0": 7, "mode": "frontier",
           "arms": arms, "pairwise": {}}
    (d / "state_ablation.json").write_text(json.dumps(rep), encoding="utf-8")
    for (arm, k), recs in (ledgers or {}).items():
        rd = d / f"run{k:02d}" / arm / "epochs"
        rd.mkdir(parents=True)
        (rd / "state_injections.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs),
            encoding="utf-8")
    return d


@pytest.fixture()
def fx(tmp_path):
    root = tmp_path / "p4_formal"
    root.mkdir()
    # ---- t_a:正例(手算数字,见模块 docstring)
    rows = [_camp_row(7 + k * 100, D1_B[k], D1_AUC[k], E_B[k], E_AUC[k],
                      E_ATOMS_CT[k], E_INJ[k]) for k in range(10)]
    _write_camp(root, "t_a", rows,
                run_atoms={0: ["S.role=admin"],
                           9: ["S.role=admin", "S.role=guest"]},
                ledgers={0: [{"epoch": 2, "status": "SKIP_R2_PROGRESS",
                              "new_atoms": ["S.role=admin"]},
                             {"goal": "S.role=guest", "path": "appmodel",
                              "status": "INJECTED", "seed_id": "x1"},
                             {"epoch": 3, "status": "SKIP_R2_PROGRESS",
                              "new_atoms": ["S.role=guest"]}]})
    _write_abl(root, "t_a",
               {"E": [20, 22, 21], "E-rand": [18, 19, 21],
                "E-always": [20, 22, 21]},
               {"E": [["A1"], ["A1", "A2"], ["A1"]],
                "E-rand": [[], ["A1"], []],
                "E-always": [["A1"], ["A1", "A2"], ["A1"]]},
               {"E": [3, 3, 2], "E-rand": [3, 2, 3], "E-always": [0, 0, 0]},
               ledgers={("E-always", 0): [
                   {"epoch": 2, "status": "INJECT_ALWAYS", "new_atoms": ["A1"]},
                   {"epoch": 3, "status": "INJECT_ALWAYS", "new_atoms": ["A2"]}],
                   ("E-always", 1): [
                   {"epoch": 2, "status": "INJECT_ALWAYS", "new_atoms": ["A1"]}],
                   ("E-always", 2): [
                   {"epoch": 3, "status": "INJECT_ALWAYS", "new_atoms": ["A2"]}]})
    # ---- t_b:null 例(战役 state_gain 零差 + 消融 trigger 零差;
    # E 行 state 字段按设计键偏斜:仅前两 run 携带)
    b_auc = [0.60, 0.61, 0.62, 0.63, 0.64]
    rows_b = [_camp_row(7 + k * 100, 30 + k, b_auc[k], 30 + k, b_auc[k],
                        1 if k < 2 else None, 0 if k < 2 else None)
              for k in range(5)]
    _write_camp(root, "t_b", rows_b,
                ledgers={k: [{"epoch": 2, "status": "SKIP_R2_PROGRESS",
                              "new_atoms": ["B1"]}] for k in range(5)})
    _write_abl(root, "t_b",
               {"E": [30, 31, 32], "E-rand": [29, 29, 30],
                "E-always": [30, 31, 32]},
               {"E": [["B1"], ["B1"], ["B1"]], "E-rand": [[], [], []],
                "E-always": [["B1"], ["B1"], ["B1"]]},
               {"E": [2, 2, 2], "E-rand": [2, 2, 2], "E-always": [2, 2, 2]},
               ledgers={("E-always", k): [
                   {"epoch": 2, "status": "INJECT_ALWAYS", "new_atoms": ["B1"]},
                   {"goal": "B1", "path": "appmodel", "status": "INJECTED",
                    "seed_id": f"s{k}"}] for k in range(3)})
    # ---- t_c:TRIGGER_NET_ZERO 措辞守卫例(微靶实况:E-always 账本有
    # INJECTED/DUPLICATE 但 0 INJECT_ALWAYS——旁路只在 new_atoms 非空时
    # 审计,覆盖代理不见新原子 → 旁路从未开火但无条件注入)
    _write_abl(root, "t_c",
               {"E": [40, 41], "E-always": [40, 41]},
               {"E": [["C1"], ["C1"]], "E-always": [["C1"], ["C1"]]},
               {"E": [2, 2], "E-always": [2, 2]},
               ledgers={("E-always", 0): [
                   {"goal": "C1", "path": "appmodel", "status": "INJECTED",
                    "seed_id": "c0"},
                   {"goal": "C1", "path": "appmodel", "status": "DUPLICATE"}],
                   ("E-always", 1): [
                   {"goal": "C1", "path": "appmodel", "status": "INJECTED",
                    "seed_id": "c1"}]})
    # ---- t_n:NO_LEDGER_EVIDENCE 例(全零差但对照臂无任何账本文件)
    _write_abl(root, "t_n",
               {"E": [50, 50], "E-rand": [50, 50]},
               {"E": [["N1"], ["N1"]], "E-rand": [["N1"], ["N1"]]},
               {"E": [1, 1], "E-rand": [1, 1]})
    # ---- 幻影靶:IR 脚手架 abl_ir_scratch(同前缀、无 state_ablation.json)
    (root / "abl_ir_scratch").mkdir()
    (root / "abl_ir_scratch" / "ir.json").write_text("{}", encoding="utf-8")
    # ---- t_orphan:仅 campaign,无消融目录(诚实缺省路径)
    _write_camp(root, "t_orphan",
                [_camp_row(7, 9, 0.40, 10, 0.45),
                 _camp_row(107, 10, 0.41, 11, 0.46)])
    return root


def _load(fx):
    from stateforge.evaluation.experiment import p4_analysis
    return p4_analysis.analyze(fx)


def _nulls(rep) -> dict:
    return {f"{n['target']}:{n['comparison']}": n for n in rep["nulls"]}


# ------------------------------------------------------- 战役:配对统计


class TestCampaign:
    def test_paired_stats_hand_computed(self, fx):
        rep = _load(fx)
        camp = rep["targets"]["t_a"]["campaign"]
        assert camp["runs"] == 10
        assert camp["arms"]["E"]["final_blocks"] == {
            "mean": 22.0, "stdev": 1.633, "n": 10}
        assert camp["arms"]["D1"]["final_blocks"]["mean"] == 20.2
        assert camp["arms"]["D1"]["final_blocks"]["stdev"] == 1.3166
        sg = camp["state_gain"]["final_blocks"]
        assert sg["mean"] == 1.8
        assert sg["median"] == 2.0
        assert sg["stdev"] == 1.2293
        assert (sg["wins"], sg["ties"], sg["losses"]) == (8, 2, 0)
        assert sg["n"] == 10
        assert sg["min"] == 0 and sg["max"] == 3
        assert sg["deltas"] == [2, 3, 1, 3, 1, 3, 2, 3, 0, 0]

    def test_auc_atoms_and_d1_proxy_note(self, fx):
        rep = _load(fx)
        camp = rep["targets"]["t_a"]["campaign"]
        auc = camp["state_gain"]["auc_blocks_norm"]
        assert auc["mean"] == pytest.approx(0.049, abs=1e-9)
        assert auc["median"] == pytest.approx(0.05, abs=1e-9)
        assert (auc["wins"], auc["ties"], auc["losses"]) == (10, 0, 0)
        assert camp["arms"]["E"]["auc_blocks_norm"]["mean"] == \
            pytest.approx(0.52, abs=1e-9)
        assert camp["arms"]["D1"]["auc_blocks_norm"]["mean"] == \
            pytest.approx(0.471, abs=1e-9)
        sa = camp["state_atoms"]
        assert sa["E_mean"] == 1.2 and sa["E_n"] == 10
        assert sa["E_injections_mean"] == 4.9
        assert sa["E_union_count"] == 2
        assert sa["E_union_atoms"] == ["S.role=admin", "S.role=guest"]
        assert sa["D1_coverage_proxy"] is None
        assert sa["D1_coverage_proxy_note"]      # 诚实缺省附注

    def test_state_keys_optional(self, fx):
        """E 行 state 字段按设计可缺(campaign 键偏斜):统计只覆盖携带 run。"""
        rep = _load(fx)
        sa = rep["targets"]["t_b"]["campaign"]["state_atoms"]
        assert sa["E_n"] == 2
        assert sa["E_mean"] == 1.0
        assert sa["E_injections_mean"] == 0.0


# ------------------------------------------------------- 诚实 null


class TestNulls:
    def test_state_gain_null_reason_from_ledger(self, fx):
        rep = _load(fx)
        n = _nulls(rep)["t_b:state_gain"]
        assert n["reason_code"] == "STATE_NEVER_ENGAGED"
        assert n["evidence"]["statuses"].get("INJECTED", 0) == 0
        assert n["evidence"]["statuses"]["SKIP_R2_PROGRESS"] == 5
        assert n["evidence"]["ledgers_found"] == 5
        assert n["blocks_mean_delta"] == 0.0
        assert n["arms"] == ["E", "D1"]
        assert "t_a:state_gain" not in _nulls(rep)   # 非零差不进 null 表

    def test_trigger_null_r2a_vacuous(self, fx):
        rep = _load(fx)
        n = _nulls(rep)["t_a:trigger_value"]
        assert n["reason_code"] == "R2A_VACUOUS"
        assert n["evidence"]["statuses"]["INJECT_ALWAYS"] == 4
        assert n["evidence"]["statuses"].get("INJECTED", 0) == 0
        assert n["blocks_mean_delta"] == 0.0
        assert n["atoms_mean_delta"] == 0.0

    def test_trigger_null_net_zero_with_writes(self, fx):
        rep = _load(fx)
        n = _nulls(rep)["t_b:trigger_value"]
        assert n["reason_code"] == "TRIGGER_NET_ZERO"
        assert n["evidence"]["statuses"]["INJECTED"] == 3
        assert n["evidence"]["statuses"]["INJECT_ALWAYS"] == 3

    def test_trigger_null_bypass_never_fired(self, fx):
        """微靶实况措辞守卫:INJECT_ALWAYS=0 且 INJECTED>0 → 不写
        "bypass engaged"(与 0 边界证据自相矛盾),改为 never-fired +
        无条件注入;evidence 计数不变。"""
        rep = _load(fx)
        n = _nulls(rep)["t_c:trigger_value"]
        assert n["reason_code"] == "TRIGGER_NET_ZERO"
        assert "bypass never fired" in n["reason"]
        assert "bypass engaged" not in n["reason"]
        assert "unconditionally" in n["reason"]
        assert n["evidence"]["statuses"].get("INJECTED") == 2
        assert "INJECT_ALWAYS" not in n["evidence"]["statuses"]
        # 有 INJECT_ALWAYS 的案例仍走旁路措辞(t_b 不回归)
        assert "bypass engaged" in _nulls(rep)["t_b:trigger_value"]["reason"]

    def test_no_ledger_evidence_case(self, fx):
        """全零差但对照臂无任何 state_injections.jsonl → NO_LEDGER_EVIDENCE
        (ledgers_found=0,statuses 空表,诚实挂起)。"""
        rep = _load(fx)
        n = _nulls(rep)["t_n:planner_contribution"]
        assert n["reason_code"] == "NO_LEDGER_EVIDENCE"
        assert n["evidence"]["ledgers_found"] == 0
        assert n["evidence"]["statuses"] == {}


# ------------------------------------------------------- 消融:配对统计


class TestAblation:
    def test_planner_contribution_hand_computed(self, fx):
        rep = _load(fx)
        ab = rep["targets"]["t_a"]["ablation"]
        assert ab["runs"] == 3
        b = ab["planner_contribution"]["blocks"]
        assert b["mean"] == 1.6667                  # 5/3,round 4
        assert b["median"] == 2.0
        assert b["stdev"] == 1.5275
        assert (b["wins"], b["ties"], b["losses"], b["n"]) == (2, 1, 0, 3)
        a = ab["planner_contribution"]["atoms"]
        assert a["mean"] == 1.0
        assert (a["wins"], a["n"]) == (3, 3)
        assert ab["arms"]["E"]["atoms_union_count"] == 2
        assert ab["arms"]["E-rand"]["atoms_union_count"] == 1
        assert ab["arms"]["E"]["atoms_union"] == ["A1", "A2"]
        assert ab["arms"]["E"]["injections_total"] == 8
        assert ab["arms"]["E-rand"]["injections_total"] == 8
        assert ab["arms"]["E-always"]["injections_total"] == 0
        assert ab["arms"]["E"]["mean_blocks"]["mean"] == 21.0
        assert ab["arms"]["E"]["mean_blocks"]["stdev"] == 1.0

    def test_planner_contribution_t_b(self, fx):
        rep = _load(fx)
        pc = rep["targets"]["t_b"]["ablation"]["planner_contribution"]
        assert pc["blocks"]["mean"] == 1.6667       # 5/3,round 4
        assert pc["blocks"]["wins"] == 3
        assert pc["atoms"]["mean"] == 1.0


# ------------------------------------------------------- 覆盖 / 只读 / 文本 / CLI


class TestCoverage:
    def test_missing_ablation_honest(self, fx):
        rep = _load(fx)
        t = rep["targets"]["t_orphan"]
        assert t["campaign"]["runs"] == 2
        assert t["ablation"] is None
        assert "t_orphan" in rep["coverage"]["targets_without_ablation"]
        assert rep["coverage"]["campaign_dirs"] == [
            str(fx / "camp_t_a"), str(fx / "camp_t_b"), str(fx / "camp_t_orphan")]
        assert rep["coverage"]["ablation_dirs"] == [
            str(fx / "abl_t_a"), str(fx / "abl_t_b"),
            str(fx / "abl_t_c"), str(fx / "abl_t_n")]
        # 无消融靶的 state_gain 仍照常配对
        assert t["campaign"]["state_gain"]["final_blocks"]["mean"] == 1.0

    def test_phantom_abl_dirs_skipped(self, fx):
        """abl_* 无 state_ablation.json(IR 脚手架 abl_ir_*)→ discover
        跳过:不进 targets、不进 ablation_dirs / ablations_without_campaign。"""
        rep = _load(fx)
        assert "ir_scratch" not in rep["targets"]
        assert all(not Path(p).name.startswith("abl_ir_")
                   for p in rep["coverage"]["ablation_dirs"])
        assert "ir_scratch" not in \
            rep["coverage"]["ablations_without_campaign"]
        assert {"t_a", "t_b", "t_c", "t_n", "t_orphan"} <= set(rep["targets"])


class TestReadOnlyAndText:
    @staticmethod
    def _tree_hash(root: Path) -> dict[str, str]:
        return {str(p.relative_to(root)):
                hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(root.rglob("*")) if p.is_file()}

    def test_analysis_never_mutates_fixture(self, fx):
        from stateforge.evaluation.experiment import p4_analysis
        before = self._tree_hash(fx)
        rep = p4_analysis.analyze(fx)
        assert p4_analysis.render_text(rep)
        assert self._tree_hash(fx) == before
        assert not (fx / "report.json").exists()

    def test_text_table_readable(self, fx):
        from stateforge.evaluation.experiment import p4_analysis
        text = p4_analysis.render_text(_load(fx))
        for needle in ("t_a", "t_b", "t_orphan", "E−D1",
                       "planner contribution", "R2A_VACUOUS",
                       "22±1.633", "mean=+1.8", "STATE_NEVER_ENGAGED"):
            assert needle in text, needle

    def test_cli_out_and_default(self, fx, tmp_path, capsys):
        from stateforge.evaluation.experiment import p4_analysis
        before = self._tree_hash(fx)
        out = tmp_path / "rep" / "report.json"
        assert p4_analysis.main([str(fx), "--out", str(out)]) == 0
        assert out.exists() and out.with_suffix(".txt").exists()
        rep = json.loads(out.read_text(encoding="utf-8"))
        assert rep["schema"].startswith("p4_analysis/")
        assert rep["targets"]["t_a"]["campaign"]["runs"] == 10
        assert self._tree_hash(fx) == before          # CLI 亦不写 fixture
        assert "t_a" in capsys.readouterr().out       # 文本表进 stdout
        # 缺省(无 --out)只打印不落盘
        assert p4_analysis.main([str(fx)]) == 0
        assert "R2A_VACUOUS" in capsys.readouterr().out
        assert not (tmp_path / "report.json").exists()

    def test_cli_empty_dir_errors(self, tmp_path):
        from stateforge.evaluation.experiment import p4_analysis
        with pytest.raises(SystemExit):
            p4_analysis.main([str(tmp_path)])
