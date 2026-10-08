"""P4-T1:轮间语料结转统一(R6)——TDD 先行,本文件先于实现。

背景(P3 终审发现的混淆因子,自 a0cc5bf 引入):轮间结转语句位于
`policy == "llm"` 门内,B/C(feedback/frontier,默认/portfolio policy)
从 epoch 2 起以空 corp_in_e{epoch} 起跑——D−B/D−C 比较混入语料重启惩罚。
R6 裁决:结转公共化(spool∪corpus,feedback 开时),公平优先于历史连续;
`_focused_corpus` 死分支(需 policy=llm∧portfolio 同时成立,矛盾)删除。

验收口径:
- B(mode=feedback,默认 policy)epoch 2 corp_in 非空且含 epoch-1 spool 冠军;
- C(mode=frontier,policy=portfolio)同;focused_in 目录不再产生;
- A(mode=off,单 epoch 连续)不受影响:无 corp_in_e2,起始语料仍为 lf_corpus;
- llm 臂注入先于结转:注入种子仍进下一轮 corp_in(T3 性质经公共路径);
- state 臂结转/注入次序回归:由 solving/test_state_seed_injector.py 既有钉
  (test_state_arm_stages_spool_champions_with_feedback)继续覆盖,不在此重复。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stateforge.analysis.static.icfg import build_icfg

STATEFUL_DIR = Path(__file__).resolve().parents[2] / "targets" / "stateful"
GATE = STATEFUL_DIR / "t_state_gate.py"

CHAMPION = b'{"json": {"op": "login", "role": "admin"}}'


@pytest.fixture(scope="module")
def ir_path(tmp_path_factory) -> Path:
    """stateful 靶的真 ICFG(驱动同源 IR)。"""
    p = tmp_path_factory.mktemp("t1ir") / "ir.json"
    build_icfg(STATEFUL_DIR).to_json(p)
    return p


def _seed_champion(wd: Path) -> str:
    """预置一个 epoch-1 spool 冠军(同 state 臂既有测法)。"""
    from stateforge.fuzzing.corpus_manager import CorpusManager
    cm = CorpusManager(wd / "epochs" / "spool")
    cid = cm.save(CHAMPION, epoch=1, new_blocks=2, new_edges=1,
                  discovered_blocks=5, discovered_edges=4)
    assert cid
    return cid


# --------------------------------------------- B/C:公共轮间结转(R6 主钉)


class TestCommonCarryover:
    def test_b_feedback_epoch2_carries_spool_champion(self, ir_path, tmp_path):
        """B 臂(feedback,默认 policy):epoch 2 起始语料非空,含 spool 冠军。
        旧行为(a0cc5bf 起)corp_in_e2 为空目录——本测试在统一前为红。"""
        from stateforge.evaluation.experiment.epoch import run_epochs

        wd = tmp_path / "wd_b"
        cid = _seed_champion(wd)
        res = run_epochs(str(GATE), str(ir_path), wd, seconds_per_epoch=2.0,
                         epochs=2, seed=7, group="B", mode="feedback",
                         use_feedback=True)
        # corpus_in 是驱动启动前拍的 staging 快照(后验看目录会被本轮
        # libFuzzer 自己落盘的新输入污染,不能作为起始语料证据)
        in2 = res["epoch_records"][1]["corpus_in"]
        assert in2["files"] >= 1, \
            "B 臂 epoch2 起始语料不得为空(语料重启惩罚,R6 已裁废除)"
        assert (wd / "corp_in_e2" / cid).read_bytes() == CHAMPION  # 冠军入选

    def test_c_portfolio_epoch2_carries_spool_champion(self, ir_path, tmp_path):
        """C 臂(frontier+portfolio):与 B/D/E 同一结转;focused_in 不再产生
        (_focused_corpus 死分支已删除,policy=portfolio 只剩公共结转)。"""
        from stateforge.evaluation.experiment.epoch import run_epochs

        wd = tmp_path / "wd_c"
        cid = _seed_champion(wd)
        res = run_epochs(str(GATE), str(ir_path), wd, seconds_per_epoch=2.0,
                         epochs=2, seed=7, group="C", mode="frontier",
                         use_feedback=True, policy="portfolio")
        in2 = res["epoch_records"][1]["corpus_in"]
        assert in2["files"] >= 1, \
            "C 臂 epoch2 起始语料不得为空(同 B,统一结转)"
        assert (wd / "corp_in_e2" / cid).read_bytes() == CHAMPION
        assert not (wd / "focused_in").exists()   # 死分支删除:无 focused 目录


# ------------------------------------------------- A:单 epoch 连续不受影响


class TestArmAUntouched:
    def test_a_off_single_epoch_no_carryover_dirs(self, ir_path, tmp_path):
        """A 臂(mode=off,单 epoch):无轮间边界——无 corp_in_e2,epoch 1
        起始语料就是 lf_corpus 本身(初始为空,无任何 staging)。"""
        from stateforge.evaluation.experiment.epoch import run_epochs

        wd = tmp_path / "wd_a"
        res = run_epochs(str(GATE), str(ir_path), wd, seconds_per_epoch=4.0,
                         epochs=1, seed=7, group="A", mode="off",
                         use_feedback=False)
        assert res["epochs"] == 1 and len(res["epoch_records"]) == 1
        assert not (wd / "corp_in_e2").exists()
        assert not (wd / "focused_in").exists()
        assert res["epoch_records"][0]["corpus_in"]["files"] == 0  # 裸跑空启动


# ------------------------------- llm 臂:注入先于公共结转(T3 性质回归钉)


class TestLLMInjectionBeforeStaging:
    def test_injected_seed_lands_in_next_corp_in(self, ir_path, tmp_path,
                                                 monkeypatch):
        """policy=llm:注入(写 corpus_dir)必须先于结转,注入种子经
        spool∪corpus 公共路径进下一轮 corp_in(替代旧 llm 门内私有结转)。"""
        from stateforge.evaluation.experiment import epoch as epoch_mod

        wd = tmp_path / "wd_d"
        seen: dict = {}

        def fake_inject(ir, workdir, corpus_dir, target, **kw):
            seen["corpus_dir"] = corpus_dir
            (corpus_dir / "llm_seed_marker").write_bytes(
                b'{"query": {"op": "llm"}}')
            return [{"status": "INJECTED", "frontier": "a->b"}]

        monkeypatch.setattr(epoch_mod, "_inject_llm_between_epochs", fake_inject)
        cid = _seed_champion(wd)
        epoch_mod.run_epochs(str(GATE), str(ir_path), wd, seconds_per_epoch=2.0,
                             epochs=2, seed=7, group="D", mode="frontier",
                             use_feedback=True, policy="llm")
        assert seen["corpus_dir"] == wd / "lf_corpus"    # 注入写的是主语料
        in2 = wd / "corp_in_e2"
        assert (in2 / "llm_seed_marker").read_bytes() == \
            b'{"query": {"op": "llm"}}'                  # 注入种子进下一轮
        assert (in2 / cid).read_bytes() == CHAMPION      # spool 冠军同轮入选
        assert (wd / "lf_corpus" / "llm_seed_marker").exists()  # 回拷主语料
