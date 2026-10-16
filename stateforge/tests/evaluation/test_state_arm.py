"""P3-T4:实验臂 E(state)+ 状态三指标与对比报告 —— TDD 先行,本文件先于实现。

验收口径(任务简报 + P3 裁决):
- 五臂接线:E = D 换 policy="state"(mode=frontier + use_feedback 同 D);
  零 LLM(无 provider、无网络,确定性);
- A–D 非回归:调用参数、metrics 行/汇总的既有键与语义不变(默认路径
  字节不变 + 既有字段完整);
- 状态三指标(演进级):state_atoms_reached / state_injections 进 recorder
  tick(可选字段,缺省键集不变)与 epoch 记录(rec["state"] / group_summary);
  派生一律走 injector 机制(reached_state_atoms / synthesize_micro_model),
  不另起炉灶;
- state_arm_report:读 five-arm run 目录,文本 + JSON,状态三指标 +
  深状态块(injector 机制派生)。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stateforge.analysis.static.icfg import build_icfg

STATEFUL_DIR = Path(__file__).resolve().parents[2] / "targets" / "stateful"
CHAIN = STATEFUL_DIR / "t_state_chain.py"
GATE = STATEFUL_DIR / "t_state_gate.py"

# 旧四臂 metrics 行/汇总键(P3-T4 改动前的形状 —— 非回归基准)
LEGACY_ROW_KEYS = ["seed", "A_tput", "B_blocks", "B_edges", "C_blocks",
                   "C_edges", "D_blocks", "D_edges"]
LEGACY_SUMMARY_KEYS = {"target", "runs", "total_time", "per_run",
                       "B_mean", "B_stdev", "C_mean", "C_stdev",
                       "D_mean", "D_stdev", "D_minus_C", "D_minus_B"}


@pytest.fixture(scope="module")
def ir_path(tmp_path_factory) -> Path:
    """stateful 靶的真 ICFG(驱动/注入器/报告同源 IR)。"""
    p = tmp_path_factory.mktemp("t4ir") / "ir.json"
    build_icfg(STATEFUL_DIR).to_json(p)
    return p


# ------------------------------------------------- 五臂接线(单元,mock run_epochs)


class TestFiveArmWiring:
    @pytest.fixture()
    def wired(self, monkeypatch, tmp_path):
        """run_epochs 替身:记调用参数,返回可区分各臂的假汇总。"""
        from stateforge.evaluation.experiment import four_arm

        calls: list[dict] = []

        def fake_run_epochs(target, ir, workdir, seconds, epochs, seed, group,
                            mode, use_feedback, **kwargs):
            calls.append({"group": group, "mode": mode,
                          "use_feedback": use_feedback,
                          "seconds": seconds, "epochs": epochs,
                          "seed": seed, "policy": kwargs.get("policy",
                                                             "reinjection")})
            depth = {"A": 1, "B": 2, "C": 3, "D": 4, "E": 6, "F": 6}[group]
            return {"total_executions": 1000, "wall_span": 2.0,
                    "final_blocks": 10 * depth, "final_edges": 5 * depth}

        monkeypatch.setattr(four_arm, "run_epochs", fake_run_epochs)
        return four_arm, calls

    def test_five_arms_in_order(self, wired, tmp_path):
        """runs=1 一次跑出 A/B/C/D/E/F 六臂,顺序与配置与旧五臂同型。"""
        four_arm, calls = wired
        s = four_arm.run_target("t.py", "ir.json", "T", tmp_path, runs=1,
                                epochs=2, seconds=1)
        assert [c["group"] for c in calls] == ["A", "B", "C", "D", "E", "F"]
        by = {c["group"]: c for c in calls}
        # A:连续裸跑(总预算,单 epoch,off,无反馈,默认 policy)
        assert by["A"]["seconds"] == 2 and by["A"]["epochs"] == 1
        assert by["A"]["mode"] == "off" and by["A"]["use_feedback"] is False
        assert by["A"]["policy"] == "reinjection"
        # B:结构新颖度
        assert by["B"]["mode"] == "feedback" and by["B"]["use_feedback"] is True
        # C:前沿定向 + portfolio
        assert by["C"]["mode"] == "frontier" and by["C"]["policy"] == "portfolio"
        # D:C + LLM 注入
        assert by["D"]["mode"] == "frontier" and by["D"]["policy"] == "llm"
        # E:D 同配置换 state 规划器注入(零 LLM)
        assert by["E"]["mode"] == "frontier"
        assert by["E"]["use_feedback"] is True
        assert by["E"]["policy"] == "state"
        for g in "BCDE":
            assert by[g]["seconds"] == 1 and by[g]["epochs"] == 2

    def test_metrics_row_has_all_five_arms(self, wired, tmp_path):
        """每 run 行 = 旧四臂键(原样)+ E/F 两臂键。"""
        four_arm, _ = wired
        s = four_arm.run_target("t.py", "ir.json", "T", tmp_path, runs=1,
                                epochs=2, seconds=1)
        row = s["per_run"][0]
        assert list(row.keys()) == LEGACY_ROW_KEYS + [
            "E_blocks", "E_edges", "F_blocks", "F_edges"]
        assert row["E_blocks"] == 60 and row["F_blocks"] == 60

    def test_summary_e_minus_d_and_legacy_intact(self, wired, tmp_path):
        """汇总 = 旧键原样 + E/F 均值与配对差;E-D 语义不变。"""
        four_arm, _ = wired
        s = four_arm.run_target("t.py", "ir.json", "T", tmp_path, runs=2,
                                epochs=2, seconds=1)
        assert LEGACY_SUMMARY_KEYS <= set(s.keys())
        assert set(s.keys()) == LEGACY_SUMMARY_KEYS | {
            "E_mean", "E_stdev", "E_minus_D",
            "F_mean", "F_stdev", "F_minus_E"}
        assert s["E_mean"] == 60.0 and s["F_mean"] == 60.0
        assert s["E_minus_D"] == 20.0          # 60-40 配对差
        assert s["F_minus_E"] == 0.0           # mock 同深 → 0
        assert s["D_minus_C"] == 10.0 and s["D_minus_B"] == 20.0  # 旧语义不变


# ------------------------------------- recorder tick 可选状态字段(C9 演进级)


class TestRecorderStateTick:
    def test_default_tick_keys_unchanged(self, tmp_path):
        """非回归:不带状态字段的 tick/summary 键集与 C9 pin 完全一致。"""
        from stateforge.fuzzing.recorder import ExperimentRecorder

        rec = ExperimentRecorder(tmp_path, group="x", epoch=0,
                                 sample_interval=0.0)
        rec.tick(0.01, 0.02, blocks=3, edges=4, saved=False, cid=None, score=0)
        rec.close()
        snap = json.loads(
            (tmp_path / "x-e0-snapshots.jsonl").read_text(encoding="utf-8"))
        assert list(snap.keys()) == ["group", "epoch", "t", "executions",
                                     "blocks", "edges", "exec_seconds",
                                     "feedback_seconds"]
        assert set(rec.summary().keys()) == {
            "group", "epoch", "wall_seconds", "executions", "exec_seconds",
            "feedback_seconds", "feedback_overhead", "throughput_exec_s",
            "blocks", "edges", "candidates_saved"}

    def test_state_fields_flow_into_tick_and_summary(self, tmp_path):
        """状态字段经 tick 可选参数进入 snapshots/summary;缺省 None 不出现;
        置一次后粘滞(演进级:轮间边界更新,非逐执行)。"""
        from stateforge.fuzzing.recorder import ExperimentRecorder

        rec = ExperimentRecorder(tmp_path, group="s", epoch=0,
                                 sample_interval=0.0)
        rec.tick(0.01, 0.02, blocks=3, edges=4, saved=False, cid=None, score=0,
                 state_atoms_reached=2, state_injections=1)
        rec.tick(0.01, 0.02, blocks=4, edges=5, saved=False, cid=None, score=0)
        rec.close()
        lines = [json.loads(x) for x in
                 (tmp_path / "s-e0-snapshots.jsonl").read_text(
                     encoding="utf-8").splitlines() if x.strip()]
        assert lines[0]["state_atoms_reached"] == 2
        assert lines[0]["state_injections"] == 1
        assert lines[1]["state_atoms_reached"] == 2      # 粘滞
        assert lines[1]["state_injections"] == 1
        summary = rec.summary()
        assert summary["state_atoms_reached"] == 2
        assert summary["state_injections"] == 1


# --------------------------------------- epoch 轮间状态摘要(演进级记录路径)


class TestEpochStateBoundary:
    def test_state_epoch_record_carries_boundary_counts(self, ir_path,
                                                        tmp_path):
        """policy=state:epoch 记录带 rec["state"](边界已达原子数+注入数),
        group_summary 带 state_atoms_reached/state_injections_total。"""
        from stateforge.evaluation.experiment.epoch import run_epochs

        wd = tmp_path / "wd_state"
        res = run_epochs(str(GATE), str(ir_path), wd, seconds_per_epoch=2.0,
                         epochs=2, seed=7, group="S", mode="feedback",
                         use_feedback=True, policy="state")
        assert len(res["epoch_records"]) == 2
        boundary = res["epoch_records"][1]["state"]
        assert boundary["epoch"] == 2
        assert isinstance(boundary["atoms_reached"], int)
        assert boundary["injected"] >= 1           # 首边界注入序列种子
        assert res["state_injections_total"] >= 1
        assert res["state_atoms_reached"] >= 1     # 末轮覆盖后判达
        assert res["state_atoms"] == ["Session.role=admin"]
        # epoch 1(无注入边界)无 state 键
        assert "state" not in res["epoch_records"][0]

    def test_default_policy_has_no_state_fields(self, ir_path, tmp_path):
        """非回归门:默认 policy 不产生任何 state 字段(A–D 路径字节不变)。"""
        from stateforge.evaluation.experiment.epoch import run_epochs

        wd = tmp_path / "wd_def"
        res = run_epochs(str(GATE), str(ir_path), wd, seconds_per_epoch=2.0,
                         epochs=2, seed=7, group="A", mode="feedback",
                         use_feedback=True)
        assert all("state" not in r for r in res["epoch_records"])
        assert not any(k.startswith("state_") for k in res)

    def test_seam_error_boundary_recorded(self, ir_path, tmp_path,
                                          monkeypatch):
        """接缝异常:boundary 记 SEAM_ERROR,epoch 记录照常完整。"""
        from stateforge.evaluation.experiment import epoch as epoch_mod

        wd = tmp_path / "wd_err"
        (wd / "epochs").mkdir(parents=True)

        def boom(*args, **kwargs):
            raise RuntimeError("seam fault")

        monkeypatch.setattr(epoch_mod,
                            "_inject_state_seeds_between_epochs", boom)
        res = epoch_mod.run_epochs(str(GATE), str(ir_path), wd,
                                   seconds_per_epoch=1.0, epochs=2, seed=7,
                                   group="E", mode="off", use_feedback=False,
                                   policy="state")
        boundary = res["epoch_records"][1]["state"]
        assert boundary["status"] == "SEAM_ERROR"
        assert "seam fault" in boundary["error"]
        assert res["state_injections_total"] == 0


# --------------------------------- P0-B:proxy/verified 双口径台账(阶段 1)


class TestEpochDualEvidenceLedger:
    def test_ledger_records_proxy_and_verified_separately(self, ir_path,
                                                          tmp_path):
        """台账三键:atoms(旧键=代理)/ proxy_reached_atoms /
        verified_reached_atoms;boundary 与末轮汇总各口径独立可查;
        global_state 无 state_evidence 块 = 未观测(None),不冒充空集。"""
        from stateforge.evaluation.experiment.epoch import (
            _final_verified_state_atoms,
            _inject_state_seeds_between_epochs,
            _state_boundary,
        )
        from stateforge.solving.state_seed_injector import synthesize_micro_model

        ir = build_icfg(STATEFUL_DIR)
        guard_lines: set = set()
        for g in synthesize_micro_model(GATE).guards:
            guard_lines |= set(g.body_lines)
        covered = [bid for bid, b in ir.blocks.items()
                   if b.file.endswith("t_state_gate.py")]
        wd = tmp_path / "wd"
        (wd / "epochs").mkdir(parents=True)
        (wd / "epochs" / "global_state.json").write_text(json.dumps({
            "covered_blocks": covered, "covered_edges": [],
            "state_evidence": {
                "evidence_version": 1, "observer": "stub",
                "verified_atoms": ["User.exists{alice@user}"],
                "verified": [], "proxy_atoms": []}}), encoding="utf-8")

        _inject_state_seeds_between_epochs(str(ir_path), wd, wd / "corp",
                                           str(GATE), epoch_num=2)
        ledger = json.loads((wd / "epochs" / "state_ledger.json")
                            .read_text(encoding="utf-8"))
        assert ledger["atoms"] == ["Session.role=admin"]      # 旧键 = 代理
        assert ledger["proxy_reached_atoms"] == ["Session.role=admin"]
        assert ledger["verified_reached_atoms"] == ["User.exists{alice@user}"]

        boundary = _state_boundary(wd, 2, 0)
        assert boundary["atoms_reached"] == 1                 # 旧字段原样
        assert boundary["proxy_atoms_reached"] == 1
        assert boundary["verified_atoms_reached"] == 1

        assert _final_verified_state_atoms(wd) == {"User.exists{alice@user}"}
        (wd / "epochs" / "global_state.json").write_text(     # 无块 = 未观测
            json.dumps({"covered_blocks": covered}), encoding="utf-8")
        assert _final_verified_state_atoms(wd) is None

    def test_run_epochs_without_evidence_has_no_verified_fields(self, ir_path,
                                                                tmp_path):
        """非回归门:无 --observe-state 的 run 不产 verified 字段
        (未观测 ≠ 观测为零),旧键 state_atoms* 原样。"""
        from stateforge.evaluation.experiment.epoch import run_epochs

        wd = tmp_path / "wd_nv"
        res = run_epochs(str(GATE), str(ir_path), wd, seconds_per_epoch=2.0,
                         epochs=2, seed=7, group="V", mode="feedback",
                         use_feedback=True, policy="state")
        assert "state_atoms_verified" not in res
        assert "state_atoms_verified_list" not in res
        assert res["state_atoms"] == ["Session.role=admin"]


# --------------------------------------------- state_arm_report(合成目录)


class TestStateArmReport:
    @pytest.fixture()
    def run_dir(self, ir_path, tmp_path) -> Path:
        """合成 five-arm run 目录:gate 靶、单 run、E 侧带真覆盖+账本;
        D 侧带浅覆盖(守卫真分支不在内)作同机制阴性对照。"""
        from stateforge.solving.state_seed_injector import (reached_state_atoms,
                                                            synthesize_micro_model)
        ir = build_icfg(STATEFUL_DIR)
        covered = [bid for bid, b in ir.blocks.items()
                   if b.file.endswith("t_state_gate.py")]
        guard_lines: set = set()
        for g in synthesize_micro_model(GATE).guards:
            guard_lines |= set(g.body_lines)
        shallow = [bid for bid, b in ir.blocks.items()
                   if b.file.endswith("t_state_gate.py")
                   and not (set(range(b.start_line, b.end_line + 1))
                            & guard_lines)]
        assert shallow and len(shallow) < len(covered)
        assert reached_state_atoms(ir, GATE,
                                   {"covered_blocks": shallow}) == set()
        rd = tmp_path / "runX"
        (rd / "ir").mkdir(parents=True)
        (rd / "run_manifest.json").write_text(json.dumps({
            "suite": "stateful", "ir": str(ir_path),
            "targets": {"t_state_gate": str(GATE)}}), encoding="utf-8")
        for arm, blocks in (("D", 30), ("E", 39)):
            arm_dir = rd / "t_state_gate" / "run00" / arm
            (arm_dir / "epochs").mkdir(parents=True)
            (arm_dir / "group_summary.json").write_text(json.dumps(
                {"group": arm, "final_blocks": blocks, "final_edges": 20,
                 "total_executions": 100}), encoding="utf-8")
        (rd / "t_state_gate" / "run00" / "E" / "epochs"
         / "global_state.json").write_text(json.dumps(
            {"covered_blocks": covered, "covered_edges": []}),
            encoding="utf-8")
        (rd / "t_state_gate" / "run00" / "E" / "epochs"
         / "state_ledger.json").write_text(json.dumps(
            {"epoch": 3, "atoms": ["Session.role=admin"]}), encoding="utf-8")
        (rd / "t_state_gate" / "run00" / "D" / "epochs"
         / "global_state.json").write_text(json.dumps(
            {"covered_blocks": shallow, "covered_edges": []}),
            encoding="utf-8")
        return rd

    def test_report_keys_and_state_triple(self, run_dir):
        """报告含五臂均值、配对差、E 状态三指标(injector 机制派生)。"""
        from stateforge.evaluation.experiment import state_arm_report as sar

        rep = sar.build_report(run_dir)
        entry = rep["targets"]["t_state_gate"]
        assert entry["runs"] == 1
        assert entry["mean"]["D"]["blocks"] == 30.0
        assert entry["mean"]["E"]["blocks"] == 39.0
        assert entry["pairwise"]["E_minus_D"] == 9.0
        st = entry["state"]["E"]
        assert st["atoms_reached"] == 1
        assert st["atoms"] == ["Session.role=admin"]
        # (from,to,trigger):login 置位 + admin_panel 过门
        assert ["POST login", "Session.role=admin", "set"] in st["transitions"]
        assert ["Session.role=admin", "POST admin_panel", "eq"] \
            in st["transitions"]
        assert st["transitions_observed"] == 2
        # 深状态块:受门函数 gate_granted 的已覆盖块(入口谓词已满足)
        assert st["deep_state_blocks"] >= 1
        assert "gate_granted" in st["deep_functions"]
        # D 覆盖代理对照:D 有浅覆盖(机制真跑)但守卫真分支未覆盖 →
        # 判达 0 —— 阴性对照,非"缺产物"的平凡真
        assert entry["state"]["D_coverage_proxy"]["atoms_reached"] == 0

    def test_report_text_and_json_output(self, run_dir, tmp_path, capsys):
        """文本渲染含关键行;JSON 落盘可回读。"""
        from stateforge.evaluation.experiment import state_arm_report as sar

        rep = sar.build_report(run_dir)
        text = sar.render_text(rep)
        assert "t_state_gate" in text
        assert "E_minus_D" in text
        assert "state_atoms_reached" in text
        out_json = tmp_path / "rep.json"
        sar.write_json(rep, out_json)
        assert json.loads(out_json.read_text(encoding="utf-8"))["targets"] \
            ["t_state_gate"]["pairwise"]["E_minus_D"] == 9.0

    def test_report_skips_arms_without_state_artifacts(self, ir_path,
                                                       tmp_path):
        """D 侧无 epochs/ 产物 → 覆盖代理为 0,不抛异常(诚实缺省)。"""
        from stateforge.evaluation.experiment import state_arm_report as sar

        rep = sar.build_report(tmp_path / "empty")   # 不存在 → 空报告不炸
        assert rep["targets"] == {}
        rd = tmp_path / "runY" / "t" / "run00" / "E"
        rd.mkdir(parents=True)
        m = sar.state_metrics(str(ir_path), str(GATE), rd)
        assert m["atoms_reached"] == 0 and m["deep_state_blocks"] == 0

    def test_merge_state_multirun_count_matches_ids(self):
        """终审前向修(I4):多 run 汇总计数由同一并集派生 —— count == len(ids)。

        两 run 深块集不相交(2+1):旧 max 聚合给 deep_state_blocks=2,
        与自身并集 deep_block_ids(len=3)自相矛盾;修后计数=3,原子/
        迁移并集口径不变。"""
        from stateforge.evaluation.experiment import state_arm_report as sar

        runs = [{"run": "run0", "atoms": ["A"],
                 "transitions": [["x", "A", "set"]],
                 "deep_state_blocks": 2, "deep_block_ids": ["b1", "b2"],
                 "deep_functions": ["f1"]},
                {"run": "run1", "atoms": ["B"],
                 "transitions": [["y", "B", "eq"]],
                 "deep_state_blocks": 1, "deep_block_ids": ["b3"],
                 "deep_functions": ["f2"]}]
        m = sar._merge_state(runs)
        assert m["atoms_reached"] == len(m["atoms"]) == 2
        assert m["transitions_observed"] == len(m["transitions"]) == 2
        assert m["deep_state_blocks"] == len(m["deep_block_ids"]) == 3
        assert m["deep_block_ids"] == ["b1", "b2", "b3"]
        assert m["deep_functions"] == ["f1", "f2"]


# ------------------------------------- E2E:小预算五臂(t_state_chain,真实)


class TestFiveArmE2E:
    def test_state_chain_five_arms_e_beats_d_on_state(self, ir_path,
                                                      tmp_path):
        """t_state_chain、runs=2、epochs=3、seconds=3:五臂完成;E 判达
        状态原子而 D 无状态机制(无注入、无账本);两臂零 LLM(无 provider
        依赖);报告在真实 run 目录上出状态三指标。"""
        from stateforge.evaluation.experiment import state_arm_report as sar
        from stateforge.evaluation.experiment.four_arm import run_target

        # 布局同 main():out/<Target>/runXX/{A..E}(报告按此层级发现)
        out = tmp_path / "out"
        s = run_target(str(CHAIN), str(ir_path), "t_state_chain",
                       out / "t_state_chain", runs=2, epochs=3, seconds=3)
        row = s["per_run"][0]
        assert {"A_tput", "B_blocks", "C_blocks", "D_blocks",
                "E_blocks"} <= set(row)
        assert s["E_minus_D"] is not None

        for k in (0, 1):
            rd = out / "t_state_chain" / f"run{k:02d}"
            for arm in "ABCDE":
                assert (rd / arm / "group_summary.json").exists(), \
                    f"run{k} {arm} 未完成"
            e_sum = json.loads((rd / "E" / "group_summary.json")
                               .read_text(encoding="utf-8"))
            d_sum = json.loads((rd / "D" / "group_summary.json")
                               .read_text(encoding="utf-8"))
            # E 拿到序列种子 → 状态原子判达;D 的 group_summary 无状态机制
            # (结构缺键;同机制的"D 是否判达"由下方报告代理断言钉)
            assert e_sum.get("state_atoms_reached", 0) == 1
            assert e_sum.get("state_atoms") == ["Session.uid=True"]
            assert d_sum.get("state_atoms_reached", 0) == 0
            assert e_sum.get("state_injections_total", 0) >= 1
            # 零 LLM:D/E 都不落 llm 注入日志(policy=state 无 provider;
            # stateful 靶无 .llm.py,llm 臂接缝静默跳过)
            for arm in ("D", "E"):
                assert not (rd / arm / "epochs" / "llm_injections.jsonl") \
                    .exists()

        rep = sar.build_report(out, target=str(CHAIN), ir_path=str(ir_path))
        entry = rep["targets"]["t_state_chain"]
        assert entry["pairwise"]["E_minus_D"] is not None
        st = entry["state"]["E"]
        assert st["atoms_reached"] == 1
        assert st["atoms"] == ["Session.uid=True"]
        assert st["deep_state_blocks"] >= 1
        assert ["POST login", "Session.uid=True", "set"] in st["transitions"]
        assert len(entry["state"]["E_runs"]) == 2
        # 同机制 D 对照(真实 run 数据,审查 Important 修):D 无注入,但
        # "是否判达"用与 E 同一覆盖代理机制算出并钉为 0 —— "同机制 E>D"
        # 不再依赖 group_summary 结构缺键的平凡真
        assert entry["state"]["D_coverage_proxy"]["atoms_reached"] == 0
