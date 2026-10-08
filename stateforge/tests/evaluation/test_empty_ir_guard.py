"""P4-T6b:空 IR 守卫(campaign / state_ablation 两入口的 main)。

P4 首跑教训:analyze 路径错误产出 0 块 IR,战役不报错、静默烧完整个
预算(约 1h)才发现。守卫契约:main() 在启动任何实验前载入 IR,
blocks=0 → parser.error(退出码 2),消息点名 IR 路径并提示套件目录
analyze 口径(program_ir.json 作为 ir 参数)。库函数 run_campaign/
run_ablation 不受影响(守卫只在 CLI 入口)。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stateforge.core.ir import ProgramIR

GATE = (Path(__file__).resolve().parents[2] / "targets" / "stateful"
        / "t_state_gate.py")


@pytest.fixture
def empty_ir(tmp_path) -> Path:
    """0 块 IR fixture(block_count 元数据 0 / functions 空,同错 analyze 路径产物)。"""
    p = tmp_path / "empty_ir.json"
    ProgramIR(metadata={"root": "nonexistent", "kind": "icfg", "version": 1,
                        "block_count": 0}).to_json(p)
    return p


@pytest.fixture
def corrupt_ir(tmp_path) -> Path:
    """坏 JSON 的 IR fixture(截断/垃圾内容——原先直接 traceback)。"""
    p = tmp_path / "corrupt_ir.json"
    p.write_text('{"blocks": {"b0": ', encoding="utf-8")
    return p


class TestCampaignEmptyIrGuard:
    def test_main_refuses_empty_ir(self, monkeypatch, tmp_path, empty_ir,
                                   capsys):
        from stateforge.evaluation.experiment import campaign as campaign_mod

        def fail_run_experiment(*a, **k):   # 守卫必须先于任何实验执行
            raise AssertionError("run_experiment reached with empty IR")

        monkeypatch.setattr(campaign_mod, "run_experiment", fail_run_experiment)
        with pytest.raises(SystemExit) as ei:
            campaign_mod.main([str(GATE), str(empty_ir),
                               "--out", str(tmp_path / "o"),
                               "--runs", "1", "--epochs", "1", "--seconds", "1"])
        assert ei.value.code == 2
        err = capsys.readouterr().err
        assert str(empty_ir) in err          # 点名 IR 路径
        assert "blocks=0" in err
        assert "analyze" in err              # 提示套件目录 analyze 口径

    def test_main_refuses_corrupt_ir_not_traceback(
            self, monkeypatch, tmp_path, corrupt_ir, capsys):
        """终审前向修:坏 JSON 的 IR → parser.error(退出码 2,点名路径),
        不再裸 traceback 进不了实验。"""
        from stateforge.evaluation.experiment import campaign as campaign_mod

        def fail_run_experiment(*a, **k):
            raise AssertionError("run_experiment reached with corrupt IR")

        monkeypatch.setattr(campaign_mod, "run_experiment", fail_run_experiment)
        with pytest.raises(SystemExit) as ei:
            campaign_mod.main([str(GATE), str(corrupt_ir),
                               "--out", str(tmp_path / "o")])
        assert ei.value.code == 2
        err = capsys.readouterr().err
        assert str(corrupt_ir) in err
        assert "无法载入" in err
        assert "analyze" in err              # 同口径提示,不止报错

    def test_main_refuses_missing_ir_not_traceback(
            self, monkeypatch, tmp_path, capsys):
        """缺文件 IR(FileNotFoundError)→ 同口径 parser.error。"""
        from stateforge.evaluation.experiment import campaign as campaign_mod

        def fail_run_experiment(*a, **k):
            raise AssertionError("run_experiment reached with missing IR")

        monkeypatch.setattr(campaign_mod, "run_experiment", fail_run_experiment)
        missing = tmp_path / "no_such_ir.json"
        with pytest.raises(SystemExit) as ei:
            campaign_mod.main([str(GATE), str(missing),
                               "--out", str(tmp_path / "o")])
        assert ei.value.code == 2
        assert str(missing) in capsys.readouterr().err


class TestStateAblationEmptyIrGuard:
    def test_main_refuses_empty_ir(self, monkeypatch, tmp_path, empty_ir,
                                   capsys):
        from stateforge.evaluation.experiment import state_ablation as sa_mod

        def fail_run_ablation(*a, **k):     # 守卫必须先于任何实验执行
            raise AssertionError("run_ablation reached with empty IR")

        monkeypatch.setattr(sa_mod, "run_ablation", fail_run_ablation)
        with pytest.raises(SystemExit) as ei:
            sa_mod.main([str(GATE), str(empty_ir),
                         "--out", str(tmp_path / "o")])
        assert ei.value.code == 2
        err = capsys.readouterr().err
        assert str(empty_ir) in err          # 点名 IR 路径
        assert "blocks=0" in err
        assert "analyze" in err              # 提示套件目录 analyze 口径

    def test_main_refuses_corrupt_ir_not_traceback(
            self, monkeypatch, tmp_path, corrupt_ir, capsys):
        """终审前向修:坏 JSON 的 IR → parser.error(退出码 2),同 campaign。"""
        from stateforge.evaluation.experiment import state_ablation as sa_mod

        def fail_run_ablation(*a, **k):
            raise AssertionError("run_ablation reached with corrupt IR")

        monkeypatch.setattr(sa_mod, "run_ablation", fail_run_ablation)
        with pytest.raises(SystemExit) as ei:
            sa_mod.main([str(GATE), str(corrupt_ir),
                         "--out", str(tmp_path / "o")])
        assert ei.value.code == 2
        err = capsys.readouterr().err
        assert str(corrupt_ir) in err
        assert "无法载入" in err
