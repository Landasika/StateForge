"""P3-T3 规划器→序列种子注入(policy=state)——TDD 先行,本文件先于实现。

验收口径(任务简报 + P3 裁决):
- 链路全真:E2E 必须走**真实注入器**(分析→goal→plan_backward→render→
  encode_sequence_seed→corpus)与**真实分派**(dispatch_seed),不许 mock 两者;
- R2 触发:未达深状态原子存在 且 上一轮无新状态节点,二者缺一不注入;
- 确定性:同输入 → 同种子字节(seeded ordering);
- budget 封顶;四臂默认路径(off/feedback/frontier/llm)不经过注入器;
- state/planning/* 与 analysis/builder.py 零修改(只消费)。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from stateforge.analysis.static.icfg import build_icfg
from stateforge.fuzzing.entry_driver import (decode_sequence_seed,
                                             dispatch_seed)

STATEFUL_DIR = Path(__file__).resolve().parents[2] / "targets" / "stateful"
MICROPLUS_DIR = Path(__file__).resolve().parents[2] / "targets" / "microplus"
GATE = STATEFUL_DIR / "t_state_gate.py"
CHAIN = STATEFUL_DIR / "t_state_chain.py"
SHOP = STATEFUL_DIR / "t_state_shop.py"

# 与 t_state_* 靶约定一致:函数名即深块标记
DEEP_MARKERS = {"t_state_gate": "gate_granted", "t_state_chain": "deep_state_profile",
                "t_state_shop": "ship_privileged"}


def _load(path: Path, tag: str):
    """按 entry_driver 同款机制加载靶模块(独立实例 = 干净先验态)。"""
    spec = importlib.util.spec_from_file_location(f"sf_t3_{tag}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ir_path(tmp_path_factory) -> tuple[Path, object]:
    """stateful 三靶的真 ICFG(驱动/注入器同源 IR)。"""
    ir = build_icfg(STATEFUL_DIR)
    p = tmp_path_factory.mktemp("t3ir") / "ir.json"
    ir.to_json(p)
    return p, ir


# ------------------------------------------------ 合成目标(多 goal / 多跳链)

SHELL = '''
import json

session = {{}}
HITS = []


def reset_state() -> None:
    session.clear()
    HITS.clear()


{funcs}


def target(data: bytes) -> None:
    try:
        seed = json.loads(data) if data else {{}}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    if isinstance(seed, dict) and isinstance(seed.get("steps"), list):
        for step in seed["steps"]:
            _handle(step)
        return
    _handle(seed)


def _handle(seed) -> None:
    if not isinstance(seed, dict):
        return
    body = seed.get("json", seed)
    if not isinstance(body, dict):
        return
    op = str(body.get("op", ""))
{branches}
'''


def _fixture_target(tmp_path: Path, branches: str, funcs: str,
                    name: str = "t_fx") -> Path:
    """写一个 M1 词汇(session 名)+ op 分派约定的合成微靶。

    branches 需自带 4 空格缩进(_handle 体内);funcs 为模块级(0 缩进)。
    """
    src = SHELL.format(branches=branches, funcs=funcs)
    p = tmp_path / f"{name}.py"
    p.write_text(src, encoding="utf-8")
    return p


def _corpus_files(corpus_dir: Path) -> dict[str, bytes]:
    return {f.name: f.read_bytes() for f in sorted(corpus_dir.iterdir())
            if f.is_file()}


def _sequence_seeds(corpus_dir: Path) -> list[list]:
    """corpus 内可解码为 C10 序列的种子(步列表)。"""
    out = []
    for data in _corpus_files(corpus_dir).values():
        steps = decode_sequence_seed(data)
        if steps is not None:
            out.append(steps)
    return out


def _ops_of(steps: list) -> list[str]:
    return [s.get("json", {}).get("op") for s in steps]


# ------------------------------------------------------- 合成:SSTG 与 goal


class TestSynthesis:
    def test_gate_sstg_shape(self):
        """login 产 role=admin 原子,admin_panel 需之——requires/produces 闭合。"""
        from stateforge.solving.state_seed_injector import synthesize_micro_model
        micro = synthesize_micro_model(GATE)
        assert micro is not None
        atoms = {"Session.role=admin"}
        assert atoms <= set(micro.sstg.nodes)
        assert {"POST login", "POST admin_panel"} <= set(micro.sstg.nodes)
        edges = {(u, v, d.get("kind")) for u, v, d in micro.sstg.edges(data=True)}
        assert ("POST login", "Session.role=admin", "produces") in edges
        assert ("POST admin_panel", "Session.role=admin", "requires") in edges

    def test_chain_truthy_goal_atom(self):
        """链靶守卫 `if uid:` → 原子 Session.uid=True(truthy 归一)。"""
        from stateforge.solving.state_seed_injector import (goal_atoms,
                                                            synthesize_micro_model)
        micro = synthesize_micro_model(CHAIN)
        assert micro is not None
        assert goal_atoms(micro.sstg) == {"Session.uid=True"}

    def test_shop_no_session_guards_no_goals(self, ir_path, tmp_path):
        """shop 的守卫在 DB 域(M1 会话词汇不可见)→ 诚实零注入。"""
        from stateforge.solving.state_seed_injector import inject_state_seeds
        ir_p, ir = ir_path
        n = inject_state_seeds(ir, SHOP, {}, tmp_path / "corp")
        assert n == 0
        assert not (tmp_path / "corp").exists() or \
            _sequence_seeds(tmp_path / "corp") == []


# ------------------------------------------------------- goal 派生(R2-a)


class TestGoalDerivation:
    def test_unreached_atom_is_goal_when_uncovered(self, ir_path):
        """空覆盖 → gate 的守卫原子即未达 goal。"""
        from stateforge.solving.state_seed_injector import reached_state_atoms
        ir_p, ir = ir_path
        assert reached_state_atoms(ir, GATE, {}) == set()

    def test_covered_deep_block_marks_atom_reached(self, ir_path):
        """守卫真分支块已覆盖 → 原子判达(覆盖代理记账)。"""
        from stateforge.solving.state_seed_injector import reached_state_atoms
        ir_p, ir = ir_path
        covered = [bid for bid, b in ir.blocks.items()
                   if b.file.endswith("t_state_gate.py")]
        assert covered
        got = reached_state_atoms(ir, GATE, {"covered_blocks": covered})
        assert got == {"Session.role=admin"}

    def test_no_injection_when_all_atoms_reached(self, ir_path, tmp_path):
        """R2-a:无未达原子 → 零注入。"""
        from stateforge.solving.state_seed_injector import inject_state_seeds
        ir_p, ir = ir_path
        covered = [bid for bid, b in ir.blocks.items()
                   if b.file.endswith("t_state_gate.py")]
        assert inject_state_seeds(ir, GATE, {"covered_blocks": covered},
                                  tmp_path / "corp") == 0


# ----------------------------------------------- 确定性与 budget(纯注入器)


class TestDeterminismAndBudget:
    def test_same_inputs_same_seed_bytes(self, ir_path, tmp_path):
        """两次独立注入(同输入)→ 逐字节相同种子集。"""
        from stateforge.solving.state_seed_injector import inject_state_seeds
        ir_p, ir = ir_path
        c1 = tmp_path / "w1" / "c"        # 独立 workdir(spool 按 corpus 父级去重)
        c2 = tmp_path / "w2" / "c"
        n1 = inject_state_seeds(ir, GATE, {}, c1)
        n2 = inject_state_seeds(ir, GATE, {}, c2)
        assert n1 == n2 == 1
        assert _corpus_files(c1) == _corpus_files(c2)

    def test_duplicate_injection_dedups_to_zero(self, ir_path, tmp_path):
        """同目录二次注入 → 内容哈希去重 → 0。"""
        from stateforge.solving.state_seed_injector import inject_state_seeds
        ir_p, ir = ir_path
        c = tmp_path / "c"
        assert inject_state_seeds(ir, GATE, {}, c) == 1
        assert inject_state_seeds(ir, GATE, {}, c) == 0

    def test_budget_caps_goal_count(self, ir_path, tmp_path):
        """3 个受门 op、budget=2 → 恰 2 个序列种子。"""
        from stateforge.solving.state_seed_injector import inject_state_seeds
        ir_p, _ = ir_path
        target = _fixture_target(
            tmp_path,
            branches='''
    if op == "login":
        session["role"] = body.get("role", "")
    elif op == "admin":
        role = session.get("role")
        if role == "admin":
            HITS.append("admin_deep")
            admin_deep()
    elif op == "dev":
        role = session.get("role")
        if role == "dev":
            HITS.append("dev_deep")
            dev_deep()
    elif op == "guest":
        role = session.get("role")
        if role == "guest":
            HITS.append("guest_deep")
            guest_deep()
''',
            funcs='''
def admin_deep():
    return "admin"


def dev_deep():
    return "dev"


def guest_deep():
    return "guest"
''')
        ir = build_icfg(tmp_path)
        c = tmp_path / "corp"
        assert inject_state_seeds(ir, target, {}, c, budget=2) == 2
        seeds = _sequence_seeds(c)
        assert len(seeds) == 2
        # 每条种子 = 生产者(login,带目标值)+ 受门 op(末步 = 当前请求)
        for steps in seeds:
            assert _ops_of(steps) == ["login", steps[-1]["json"]["op"]]
            assert steps[0]["json"]["role"] == steps[1]["json"]["op"]


# --------------------------------------- 多跳链(requires 传递性 → 深度规划)


class TestMultihopChain:
    def test_plan_chains_through_requires_transitivity(self, ir_path, tmp_path):
        """register→post→view:goal=view 的守卫原子,plan_backward 反向链出
        两级生产者,末步补受门 op → 三步序列打开深块。"""
        from stateforge.solving.state_seed_injector import inject_state_seeds
        ir_p, _ = ir_path
        target = _fixture_target(
            tmp_path,
            branches='''
    if op == "register":
        session["uid"] = body.get("user", "")
    elif op == "post":
        uid = session.get("uid")
        if uid:
            session["posted"] = body.get("title", "")
    elif op == "view":
        posted = session.get("posted")
        if posted:
            HITS.append("deep")
            deep()
''',
            funcs='''
def deep():
    return "deep"
''', name="t_multihop")
        ir = build_icfg(tmp_path)
        c = tmp_path / "corp"
        assert inject_state_seeds(ir, target, {}, c) >= 1
        seeds = {tuple(_ops_of(s)) for s in _sequence_seeds(c)}
        assert ("register", "post", "view") in seeds   # 多跳链存在
        # 且经真实分派确实打开深块(独立干净实例)
        mod = _load(target, "multihop")
        for data in _corpus_files(c).values():
            if decode_sequence_seed(data) is not None:
                dispatch_seed(mod.target, data)
        assert "deep" in mod.HITS


# ------------------------------------------------------ E2E:真实注入器+分派


class TestE2EDispatch:
    def test_injected_seed_opens_gate_through_real_dispatch(self, ir_path,
                                                            tmp_path):
        """注入器产出的种子字节经真实 dispatch_seed 打开 401 门(无 mock)。"""
        from stateforge.solving.state_seed_injector import inject_state_seeds
        ir_p, ir = ir_path
        c = tmp_path / "corp"
        assert inject_state_seeds(ir, GATE, {}, c) == 1
        seeds = _sequence_seeds(c)
        assert len(seeds) == 1
        assert _ops_of(seeds[0]) == ["login", "admin_panel"]
        assert seeds[0][0]["json"]["role"] == "admin"
        mod = _load(GATE, "gate_e2e")
        files = _corpus_files(c)
        for data in files.values():
            if decode_sequence_seed(data) is not None:
                dispatch_seed(mod.target, data)
        assert mod.HITS == ["login_ok", "gate_granted"]
        assert mod._SESSION.get("role") == "admin"


# --------------------------------------------- epoch 轮间接缝(R2-b 触发)


class TestEpochSeam:
    def _workdir(self, tmp_path, covered_blocks) -> Path:
        wd = tmp_path / "wd"
        (wd / "epochs").mkdir(parents=True)
        (wd / "epochs" / "global_state.json").write_text(
            json.dumps({"blocks": covered_blocks,
                        "covered_blocks": covered_blocks}), encoding="utf-8")
        (wd / "lf_corpus").mkdir()
        return wd

    def test_r2_progress_last_epoch_skips_injection(self, ir_path, tmp_path):
        """上轮有新状态节点(账本差分非空)→ 不注入,记账不回退。"""
        from stateforge.evaluation.experiment.epoch import (
            _inject_state_seeds_between_epochs)
        ir_p, ir = ir_path
        covered = [bid for bid, b in ir.blocks.items()
                   if b.file.endswith("t_state_gate.py")]
        wd = self._workdir(tmp_path, covered)
        n = _inject_state_seeds_between_epochs(str(ir_p), wd, wd / "lf_corpus",
                                               str(GATE), epoch_num=2)
        assert n == 0
        assert _sequence_seeds(wd / "lf_corpus") == []
        ledger = json.loads((wd / "epochs" / "state_ledger.json").read_text())
        assert ledger["atoms"] == ["Session.role=admin"]
        audit = (wd / "epochs" / "state_injections.jsonl").read_text()
        assert "SKIP_R2_PROGRESS" in audit

    def test_r2_stall_injects_and_ledger_persists(self, ir_path, tmp_path):
        """上轮无新状态节点且存在未达原子 → 注入,账本更新。"""
        from stateforge.evaluation.experiment.epoch import (
            _inject_state_seeds_between_epochs)
        ir_p, ir = ir_path
        wd = self._workdir(tmp_path, [])
        assert _inject_state_seeds_between_epochs(str(ir_p), wd, wd / "lf_corpus",
                                                  str(GATE), epoch_num=2) == 1
        assert len(_sequence_seeds(wd / "lf_corpus")) == 1
        ledger = json.loads((wd / "epochs" / "state_ledger.json").read_text())
        assert ledger["atoms"] == []

    def test_missing_global_state_no_injection(self, ir_path, tmp_path):
        """mode=off 等无 global_state.json → 平级跳过(llm 注入同款守卫)。"""
        from stateforge.evaluation.experiment.epoch import (
            _inject_state_seeds_between_epochs)
        ir_p, _ = ir_path
        wd = tmp_path / "wd"
        wd.mkdir()
        assert _inject_state_seeds_between_epochs(str(ir_p), wd, wd / "lf_corpus",
                                                  str(GATE), epoch_num=2) == 0

    def test_default_policy_never_invokes_injector(self, tmp_path):
        """四臂非回归门:默认 policy(reinjection)全跑完,零 state 注入痕迹。"""
        from stateforge.evaluation.experiment.epoch import run_epochs
        target = MICROPLUS_DIR / "deep_branch.py"
        ir = build_icfg(MICROPLUS_DIR)
        ir_p = tmp_path / "ir.json"
        ir.to_json(ir_p)
        wd = tmp_path / "wd_default"
        run_epochs(str(target), str(ir_p), wd, seconds_per_epoch=2.0, epochs=2,
                   seed=7, group="A", mode="feedback", use_feedback=False)
        assert not (wd / "epochs" / "state_injections.jsonl").exists()
        assert not (wd / "epochs" / "state_ledger.json").exists()
        assert _sequence_seeds(wd / "lf_corpus") == []

    def test_state_arm_stages_spool_champions_with_feedback(self, ir_path,
                                                            tmp_path):
        """修 1(Important):state+use_feedback 的轮间结转对齐 llm 臂——
        spool 反馈冠军 ∪ lf_corpus 都进下一轮起始语料(不得只拷 lf_corpus,
        否则 state 臂在语料结转上严格弱于其它多轮臂)。"""
        from stateforge.evaluation.experiment.epoch import run_epochs
        from stateforge.fuzzing.corpus_manager import CorpusManager
        ir_p, _ = ir_path
        wd = tmp_path / "wd_spool"
        champion = b'{"json": {"op": "login", "role": "admin"}}'
        cm = CorpusManager(wd / "epochs" / "spool")     # 预置一个冠军种子
        cid = cm.save(champion, epoch=1, new_blocks=2, new_edges=1,
                      discovered_blocks=5, discovered_edges=4)
        assert cid
        run_epochs(str(GATE), str(ir_p), wd, seconds_per_epoch=2.0, epochs=2,
                   seed=7, group="S", mode="feedback", use_feedback=True,
                   policy="state")
        in2 = wd / "corp_in_e2"
        assert (in2 / cid).read_bytes() == champion     # spool 冠军入选下轮
        assert _sequence_seeds(in2)                     # 注入的序列种子也入选

    def test_seam_error_audited_and_run_continues(self, ir_path, tmp_path,
                                                  monkeypatch):
        """修 2(Minor):接缝异常不再无声吞——留 SEAM_ERROR 审计行,
        且注入失败照常跑下一轮(与 llm 接缝容错同形)。"""
        from stateforge.evaluation.experiment import epoch as epoch_mod
        ir_p, _ = ir_path
        wd = tmp_path / "wd_err"
        (wd / "epochs").mkdir(parents=True)

        def boom(*args, **kwargs):
            raise RuntimeError("seam fault")

        monkeypatch.setattr(epoch_mod, "_inject_state_seeds_between_epochs", boom)
        res = epoch_mod.run_epochs(str(GATE), str(ir_p), wd,
                                   seconds_per_epoch=1.0, epochs=2, seed=7,
                                   group="E", mode="off", use_feedback=False,
                                   policy="state")
        assert len(res["epoch_records"]) == 2           # 两轮都跑完
        audit = (wd / "epochs" / "state_injections.jsonl").read_text()
        assert "SEAM_ERROR" in audit and "seam fault" in audit


# ------------------------------------------------- E2E:完整轮间闭环(旗舰)


class TestE2EFullLoop:
    def test_policy_state_cycle_covers_deep_block(self, ir_path, tmp_path):
        """旗舰 E2E:t_state_gate + policy=state、无初始序列种子 →
        轮间注入器派生 login→admin 链落盘 → 下一轮真实执行 → 深块入覆盖。"""
        from stateforge.evaluation.experiment.epoch import run_epochs
        ir_p, ir = ir_path
        wd = tmp_path / "wd_state"
        result = run_epochs(str(GATE), str(ir_p), wd, seconds_per_epoch=3.0,
                            epochs=3, seed=7, group="T3", mode="feedback",
                            use_feedback=True, policy="state")
        assert len(result["epoch_records"]) == 3

        # 1) 轮间确有注入(审计),goal = 未达守卫原子
        audit = [json.loads(x) for x in
                 (wd / "epochs" / "state_injections.jsonl").read_text().splitlines()
                 if x.strip()]
        injected = [r for r in audit if r.get("status") == "INJECTED"]
        assert injected and all(r["goal"] == "Session.role=admin"
                                for r in injected)

        # 2) 注入的序列种子确实在语料里,且形如 login(role=admin)→admin_panel
        seeds = _sequence_seeds(wd / "lf_corpus")
        assert ["login", "admin_panel"] in [_ops_of(s) for s in seeds]

        # 3) 深块被真实覆盖(gate_granted 函数块进入全局覆盖)
        state = json.loads((wd / "epochs" / "global_state.json").read_text())
        covered = set(state.get("covered_blocks") or state.get("blocks") or [])
        deep = {bid for bid in covered
                if ir.blocks.get(bid) is not None
                and ir.blocks[bid].file.endswith("t_state_gate.py")
                and ir.blocks[bid].function == "gate_granted"}
        assert deep, "深块 gate_granted 未被覆盖:序列种子未打开 401 门"
