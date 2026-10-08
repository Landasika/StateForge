"""P4-T4b:campaign `--groups` 子集参数(正式战役只跑问题相关臂 D1,E)。

验收口径(任务简报):
- `--groups "D1,E"` 接受逗号分隔的 GROUPS 子集,逐名校验;非法名 →
  parser.error 退出码 2 并列出全部有效组名;
- 子集经 `run_campaign(..., groups=...)` 传至
  `run_experiment(..., groups=...)`(runner 侧新增可选参,缺省 None =
  全 GROUPS,GROUPS 常量不变);
- 缺省(None)路径逐字不变:campaign 对 run_experiment 仍以既有 6 参
  形式调用(不带 groups kwarg,既有 mock 替身零改动);
- 子集运行时聚合/questions 对缺组诚实缺省(不造空行、不 0 填充),
  summary.json / curves.csv 照常落盘。
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
    p = tmp_path_factory.mktemp("t4bir") / "ir.json"
    build_icfg(STATEFUL_DIR).to_json(p)
    return p


class TestGroupsSubsetPropagation:
    def test_subset_reaches_run_experiment_and_summary_tolerates_missing(
            self, monkeypatch, tmp_path, ir_path):
        """groups=("D1","E") 逐 run 透传至 run_experiment;campaign 聚合
        只含 D1/E 行,涉缺组的问题诚实缺省,E−D1 配对照常产出。"""
        from stateforge.evaluation.experiment import campaign as campaign_mod

        calls: list[tuple | None] = []

        def fake_run_experiment(target, ir, run_dir, epochs, seconds, seed,
                                groups=None):
            # mock 口径:子集运行 → 只落子集目录(与真 runner 的
            # groups 门控行为同形),且 mock 曲线空 → AUC 恒 0 确定性
            calls.append(tuple(groups) if groups is not None else None)
            run_dir.mkdir(parents=True)
            depth = {"D1": 50, "E": 65}
            groups_out = {}
            for name in groups:
                g = {"total_executions": 100, "wall_span": 2.0,
                     "final_blocks": depth[name], "final_edges": 5,
                     "total_feedback_seconds": 0.0}
                if name == "E":                     # policy=state 的状态汇总
                    g["state_injections_total"] = 2
                    g["state_atoms_reached"] = 1
                groups_out[name] = g
                gd = run_dir / name
                gd.mkdir()
                (gd / "group_summary.json").write_text(json.dumps(
                    {"epoch_records": [
                        {"wall": {"start_offset": 0.0}, "snapshots": []}],
                     **g}), encoding="utf-8")
            return {"groups": groups_out}

        monkeypatch.setattr(campaign_mod, "run_experiment",
                            fake_run_experiment)
        s = campaign_mod.run_campaign(str(GATE), str(ir_path),
                                      tmp_path / "camp", runs=2, epochs=2,
                                      seconds=3.0, seed0=7,
                                      groups=("D1", "E"))

        # 透传:每个 run 的 run_experiment 调用都收到同一子集
        assert calls == [("D1", "E"), ("D1", "E")]

        # 聚合诚实缺省:只有 D1/E 行(不造六个缺组空行)
        assert set(s["aggregate"]) == {"D1", "E"}
        assert s["aggregate"]["E"]["final_blocks"]["mean"] == 65
        assert s["aggregate"]["E"]["state_injections_total"]["n"] == 2

        # questions:涉缺组的键整体缺省;D1/E 配对(state_gain)照常
        assert "tracing_overhead" not in s["questions"]
        assert "restart_penalty_final_blocks" not in s["questions"]
        assert "feedback_gain_final_blocks" not in s["questions"]
        assert "portfolio_gain_final_blocks" not in s["questions"]
        assert s["questions"]["state_gain_final_blocks"]["mean"] == 15  # 65−50
        assert s["questions"]["paired_deltas"] == {"E_minus_D1": [0.0, 0.0]}

        # 落盘完好:summary.json 可读且与返回值一致;curves.csv 含表头
        on_disk = json.loads((tmp_path / "camp" / "summary.json")
                             .read_text(encoding="utf-8"))
        assert set(on_disk["aggregate"]) == {"D1", "E"}
        assert (tmp_path / "camp" / "curves.csv").read_text(
            encoding="utf-8").startswith("group,t,blocks_mean")

        # 缺省(groups=None)路径零漂移:run_experiment 仍以既有 6 参
        # 形式调用(替身签名不带 groups → 多传 kwarg 即 TypeError);
        # 空报告(无组目录)时聚合/questions 诚实缺省、落盘不崩。
        strict_calls: list[int] = []

        def strict_run_experiment(target, ir, run_dir, epochs, seconds,
                                  seed):                     # 无 groups 参
            strict_calls.append(seed)
            run_dir.mkdir(parents=True)
            return {"groups": {}}

        monkeypatch.setattr(campaign_mod, "run_experiment",
                            strict_run_experiment)
        s = campaign_mod.run_campaign(str(GATE), str(ir_path),
                                      tmp_path / "camp_default", runs=1,
                                      epochs=1, seconds=1.0, seed0=7)
        assert len(strict_calls) == 1
        assert s["aggregate"] == {}
        assert s["questions"]["paired_deltas"] == {}


class TestGroupsArgValidation:
    def test_invalid_name_exits_2_listing_valid_names(self, tmp_path, capsys):
        """--groups "D1,X":parser.error → 退出码 2,stderr 列出无效名
        与全部有效组名(GROUPS)。"""
        from stateforge.evaluation.experiment import campaign as campaign_mod
        from stateforge.evaluation.experiment.runner import GROUPS

        with pytest.raises(SystemExit) as ei:
            campaign_mod.main(["t.py", "ir.json",
                               "--out", str(tmp_path / "o"),
                               "--groups", "D1,X"])
        assert ei.value.code == 2
        err = capsys.readouterr().err
        assert "X" in err
        for g in GROUPS:                         # 有效名单完整列出
            assert g in err
