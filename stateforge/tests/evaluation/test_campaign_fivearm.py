"""P4-T2:campaign 七组 → 八组(E 臂接入)+ synthesize_micro_model memo。

验收口径(任务简报 + P4 计划):
- runner.GROUPS 追加 "E",配置与 four_arm 的 E 逐字同语义
  (mode=frontier + use_feedback=True + policy=state,零 LLM);
- campaign 聚合含 E 行;group_summary 的状态字段
  (state_atoms_reached / state_injections_total)出现即进聚合表;
- E 的配对对照 = D1(campaign 无 LLM 臂,four_arm 的 E−D 无对应物;
  D1 = 纯 directed frontier,与 E 唯一差异 = policy=state → 配对差即
  state 注入净贡献);questions 增 state_gain_* 与 paired_deltas.E_minus_D1;
- synthesize_micro_model memo:同 resolved target → 同一模型对象(身份),
  str/Path 归一同键;cache_clear 后真重算(内容等价,非同一对象)。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stateforge.analysis.static.icfg import build_icfg

STATEFUL_DIR = Path(__file__).resolve().parents[2] / "targets" / "stateful"
GATE = STATEFUL_DIR / "t_state_gate.py"


@pytest.fixture(scope="module")
def ir_path(tmp_path_factory) -> Path:
    """stateful 靶的真 ICFG(run_campaign 的 ProgramIR 输入)。"""
    p = tmp_path_factory.mktemp("t2ir") / "ir.json"
    build_icfg(STATEFUL_DIR).to_json(p)
    return p


# ------------------------------------------- runner 接线(mock run_epochs)


class TestRunnerEWiring:
    @pytest.fixture()
    def wired(self, monkeypatch):
        from stateforge.evaluation.experiment import runner

        calls: list[dict] = []

        def fake_run_epochs(target, ir, workdir, seconds, epochs, seed,
                            group, mode, use_feedback, t0=None, **kwargs):
            calls.append({"group": group, "mode": mode, "seconds": seconds,
                          "epochs": epochs, "seed": seed, "t0": t0,
                          "use_feedback": use_feedback,
                          "policy": kwargs.get("policy", "reinjection")})
            return {}

        monkeypatch.setattr(runner, "run_epochs", fake_run_epochs)
        return runner, calls

    def test_groups_contains_e_last(self, wired):
        """GROUPS 以 F 收尾(E 后接 F,与 run_experiment 派生序一致)。"""
        runner, _ = wired
        assert runner.GROUPS[-1] == "F"
        assert runner.GROUPS == ("A_native", "A_measure", "B", "C",
                                 "D1", "D2R", "D2P", "E", "F")

    def test_e_group_config_mirrors_four_arm(self, wired, tmp_path):
        """E = frontier + feedback + policy=state(与 D1 同预算同 seed 同 t0),
        零 LLM;D1/D2R/D2P 配置不因 E 接入而变(非回归)。"""
        runner, calls = wired
        runner.run_experiment("t.py", "ir.json", tmp_path, epochs=3,
                              seconds_per_epoch=5.0, seed=7)
        by = {c["group"]: c for c in calls}
        assert [c["group"] for c in calls] == list(runner.GROUPS)
        assert by["E"]["mode"] == "frontier"
        assert by["E"]["use_feedback"] is True
        assert by["E"]["policy"] == "state"
        assert by["E"]["seconds"] == 5.0 and by["E"]["epochs"] == 3
        assert by["E"]["seed"] == 7 and by["E"]["t0"] == by["D1"]["t0"]
        # 既有七组配置逐字不变(P4-T1 后口径)
        assert by["D1"]["policy"] == "reinjection"
        assert by["D2R"]["policy"] == "reinjection"
        assert by["D2P"]["policy"] == "portfolio"
        assert by["C"]["mode"] == "feedback"


# ------------------------------- campaign 聚合(mock run_experiment,快)


class TestCampaignEAggregation:
    DEPTH = {"A_native": 10, "A_measure": 20, "B": 30, "C": 40,
             "D1": 50, "D2R": 60, "D2P": 70, "E": 65, "F": 65}

    @pytest.fixture()
    def summary(self, monkeypatch, tmp_path, ir_path):
        """run_experiment 替身:每组落一个可读的 group_summary.json
        (E 带状态字段),blocks 按组深度可区分 → 配对差可断言。"""
        from stateforge.evaluation.experiment import campaign as campaign_mod
        from stateforge.evaluation.experiment import runner

        def fake_run_experiment(target, ir, run_dir, epochs, seconds, seed):
            run_dir.mkdir(parents=True)
            groups = {}
            for name in runner.GROUPS:
                g = {"total_executions": 100, "wall_span": 2.0,
                     "final_blocks": self.DEPTH[name], "final_edges": 5,
                     "total_feedback_seconds": 0.0}
                if name == "E":                     # policy=state 的状态汇总
                    g["state_injections_total"] = 2
                    g["state_atoms_reached"] = 1
                groups[name] = g
                gd = run_dir / name
                gd.mkdir()
                (gd / "group_summary.json").write_text(json.dumps(
                    {"epoch_records": [{"wall": {"start_offset": 0.0},
                                        "snapshots": []}], **g}),
                    encoding="utf-8")
            return {"groups": groups}

        monkeypatch.setattr(campaign_mod, "run_experiment",
                            fake_run_experiment)
        return campaign_mod.run_campaign(str(GATE), str(ir_path),
                                         tmp_path / "camp", runs=2,
                                         epochs=2, seconds=3.0, seed0=7)

    def test_aggregate_has_e_row_with_state_fields(self, summary):
        """聚合表含 E 行;E 的状态字段(出现即收)成 _stats 口径(n=runs);
        非 state 臂(D1)不含状态键。"""
        agg = summary["aggregate"]
        assert set(agg) == {"A_native", "A_measure", "B", "C",
                            "D1", "D2R", "D2P", "E", "F"}
        assert agg["E"]["final_blocks"]["mean"] == 65
        assert agg["E"]["state_injections_total"] == {
            "mean": 2, "median": 2, "stdev": 0.0, "n": 2,
            "iqr": [2, 2]}
        assert agg["E"]["state_atoms_reached"]["n"] == 2
        assert "state_injections_total" not in agg["D1"]

    def test_e_minus_d1_paired_diff_present(self, summary):
        """E−D1(state 注入净贡献)进 questions:state_gain_*(配对差 stats)
        + paired_deltas.E_minus_D1 逐 run 列表。"""
        q = summary["questions"]
        assert q["state_gain_final_blocks"]["mean"] == 15   # 65 − 50
        assert q["state_gain_final_blocks"]["n"] == 2
        # mock 曲线空 → 全组 AUC 恒 0 → 配对差 stats 确定性(钉全值)
        assert q["state_gain_auc_norm"] == {"mean": 0.0, "median": 0.0,
                                            "stdev": 0.0,
                                            "iqr": [0.0, 0.0], "n": 2}
        assert q["paired_deltas"]["E_minus_D1"] == [0.0, 0.0]  # mock 曲线空
        # 既有配对键原样(非回归)
        assert set(q["paired_deltas"]) == {"D1_minus_C", "D2P_minus_D2R",
                                           "E_minus_D1"}


# ------------------------------------------------- 聚合抗键偏斜(P4-T2 修)


class TestCampaignKeySkew:
    """per-run 可选键的键偏斜:state_atoms_reached 按设计可缺
    (epoch.py _final_state_atoms 无覆盖态/异常 → None → 键缺省)。
    修前聚合键集取 per_run[0]:正偏斜(run0 有、后续 run 缺)→ 全部
    fuzzing 跑完后 KeyError,summary/curves 均不落盘;反偏斜(run0 缺)
    → 字段被静默丢弃。修后 = 各 run 键集并集 + 缺 run 过滤取值。
    """

    def test_optional_state_key_skew_both_directions(self, monkeypatch,
                                                     tmp_path, ir_path):
        from stateforge.evaluation.experiment import campaign as campaign_mod
        from stateforge.evaluation.experiment import runner

        def make_fake(has_atoms_per_run):
            def fake_run_experiment(target, ir, run_dir, epochs, seconds,
                                    seed):
                run_dir.mkdir(parents=True)
                k = (seed - 7) // 100                # run 序号(seed0=7)
                groups = {}
                for name in runner.GROUPS:
                    g = {"total_executions": 100, "wall_span": 2.0,
                         "final_blocks": 50, "final_edges": 5,
                         "total_feedback_seconds": 0.0}
                    if name == "E":
                        g["state_injections_total"] = 2    # policy=state 恒有
                        if has_atoms_per_run[k]:           # 判达按设计可缺
                            g["state_atoms_reached"] = 1
                    groups[name] = g
                    gd = run_dir / name
                    gd.mkdir()
                    (gd / "group_summary.json").write_text(json.dumps(
                        {"epoch_records": [
                            {"wall": {"start_offset": 0.0}, "snapshots": []}],
                         **g}), encoding="utf-8")
                return {"groups": groups}
            return fake_run_experiment

        def run_with(flags, out):
            monkeypatch.setattr(campaign_mod, "run_experiment",
                                make_fake(flags))
            return campaign_mod.run_campaign(str(GATE), str(ir_path), out,
                                             runs=2, epochs=2, seconds=3.0,
                                             seed0=7)

        # 正偏斜:run0 判达、run1 缺键 → 聚合完成,stats 只算有的 run
        agg = run_with([True, False], tmp_path / "skew_fwd")["aggregate"]["E"]
        assert agg["state_injections_total"]["n"] == 2      # 恒有键:全 run
        assert agg["state_atoms_reached"]["n"] == 1         # 缺 run 被过滤
        assert agg["state_atoms_reached"]["mean"] == 1

        # 反偏斜:run0 缺键 → 字段从 run1 恢复,不静默丢弃
        agg = run_with([False, True], tmp_path / "skew_inv")["aggregate"]["E"]
        assert "state_atoms_reached" in agg
        assert agg["state_atoms_reached"]["n"] == 1
        assert agg["state_atoms_reached"]["mean"] == 1


# ------------------------------- synthesize_micro_model memo(同文件收口)


class TestSynthesizeMemo:
    def test_same_target_returns_cached_object(self):
        """同 resolved target 两次调用 → 同一模型对象;str/Path 归一同键。"""
        from stateforge.solving import state_seed_injector as ssi
        ssi.synthesize_micro_model.cache_clear()
        m1 = ssi.synthesize_micro_model(GATE)
        assert m1 is not None
        assert ssi.synthesize_micro_model(GATE) is m1
        assert ssi.synthesize_micro_model(str(GATE)) is m1

    def test_cache_clear_recomputes_equal_content(self):
        """cache_clear 后真重算(非同一对象),内容等价(确定性不因 memo 破)。"""
        from stateforge.solving import state_seed_injector as ssi
        ssi.synthesize_micro_model.cache_clear()
        m1 = ssi.synthesize_micro_model(GATE)
        ssi.synthesize_micro_model.cache_clear()
        m2 = ssi.synthesize_micro_model(GATE)
        assert m2 is not m1
        assert [g.atom for g in m2.guards] == [g.atom for g in m1.guards]
        assert sorted(m2.ops) == sorted(m1.ops)
        assert ({e for e in m1.sstg.edges}
                == {e for e in m2.sstg.edges})
