"""P4-T4 消融臂 E-rand(随机序列)/ E-always(恒注入)——TDD 先行,本文件先于实现。

验收口径(任务简报 + R8/R9 裁决):
- R8:E-rand = 同 SSTG 动作集上均匀采样的**合法前缀序列**(同靶、同
  budget、同 R2 触发语义)——隔离规划器贡献;非随机字节、非异词表;
  seeded RNG,逐 seed 确定性;
- R8 消融边界:E-rand 渲染**不带规划器知识**(无 goal 值绑定、无
  _JSON_FILL、无链完备化)——规划器贡献 = 链选择 + 值绑定 + 链补全;
- E-always:同注入机制,R2-(b) 旁路(未达原子存在即逐边界注入,budget
  封顶;R2-(a)「未达原子存在」保留);
- 接缝改动最小且门控:trigger/injector 缺省时 E 路径行为逐字节不变
  (签名缺省值钉死 + 既有 test_state_seed_injector.py 全绿为非回归门);
- R9:独立脚本 state_ablation.py(不塞 four_arm);三臂同靶/同 budget/
  同触发语义;报告形状 = state_arm_report 同款(blocks/edges + 状态三
  指标)+ E−E-rand / E−E-always 配对差。
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from stateforge.analysis.static.icfg import build_icfg
from stateforge.fuzzing.entry_driver import decode_sequence_seed

STATEFUL_DIR = Path(__file__).resolve().parents[2] / "targets" / "stateful"
VULNSHOP = Path(__file__).resolve().parents[2] / "targets" / "vulnshop"
GATE = STATEFUL_DIR / "t_state_gate.py"

NEUTRAL = "x"          # 与注入器同一中性绑定(非字面值槽位)


@pytest.fixture(scope="module")
def ir_path(tmp_path_factory) -> tuple[Path, object]:
    """stateful 靶的真 ICFG(驱动/注入器/消融同源 IR)。"""
    ir = build_icfg(STATEFUL_DIR)
    p = tmp_path_factory.mktemp("t4abir") / "ir.json"
    ir.to_json(p)
    return p, ir


@pytest.fixture(scope="module")
def vs_ir(tmp_path_factory) -> tuple[Path, object]:
    """vulnshop 目录的真 ICFG(AppModel 路径侧)。"""
    ir = build_icfg(VULNSHOP)
    p = tmp_path_factory.mktemp("t4abvir") / "ir.json"
    ir.to_json(p)
    return p, ir


def _corpus_files(corpus_dir: Path) -> dict[str, bytes]:
    return {f.name: f.read_bytes() for f in sorted(corpus_dir.iterdir())
            if f.is_file()}


def _sequence_seeds(corpus_dir: Path) -> list[list]:
    out = []
    for data in _corpus_files(corpus_dir).values():
        steps = decode_sequence_seed(data)
        if steps is not None:
            out.append(steps)
    return out


def _ops_of(steps: list) -> list[str]:
    return [s.get("json", {}).get("op") for s in steps]


# 多原子合成靶(3 个受门 op ⇒ 3 个 requires 原子;E-always 的钉扎场景)
_SHELL = '''import json

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

_FUNCS = '''
def admin_deep():
    return "admin"


def dev_deep():
    return "dev"


def guest_deep():
    return "guest"
'''

_BRANCHES = '''
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
'''


@pytest.fixture(scope="module")
def multi_atom(tmp_path_factory) -> tuple[Path, Path, object]:
    """3 受门 op 微靶 + 其 IR(模块级缓存:靶文件不改写)。"""
    d = tmp_path_factory.mktemp("t4fix")
    target = d / "t_multi.py"
    target.write_text(_SHELL.format(branches=_BRANCHES, funcs=_FUNCS),
                      encoding="utf-8")
    ir = build_icfg(d)
    p = d / "ir.json"
    ir.to_json(p)
    return target, p, ir


def _cover_blocks(ir, target: Path, atoms_wanted: set[str]) -> list[str]:
    """给定原子判达所需的覆盖块(真分支行集 ∩ 靶文件块)——覆盖代理同源。"""
    from stateforge.solving.state_seed_injector import synthesize_micro_model
    micro = synthesize_micro_model(target)
    lines: set[int] = set()
    for g in micro.guards:
        if g.atom in atoms_wanted:
            lines |= set(g.body_lines)
    return [bid for bid, b in ir.blocks.items()
            if b.file.endswith(target.name)
            and set(b.line_numbers or range(b.start_line, b.end_line + 1)) & lines]


# ------------------------------------------- E-rand:同动作集/合法性/确定性


class TestRandSamplerMicro:
    def test_seeds_are_valid_c10_from_same_action_set(self, ir_path, tmp_path):
        """R8:E-rand 种子 = 合法 C10 序列,op ∈ 同一 SSTG 动作集(非异词表)。"""
        from stateforge.evaluation.experiment.state_ablation import (
            action_set, inject_random_seeds)
        from stateforge.solving.state_seed_injector import synthesize_micro_model
        ir_p, _ = ir_path
        c = tmp_path / "c"
        assert inject_random_seeds(ir_p, GATE, {}, c, budget=3, seed="d1") == 3
        seeds = _sequence_seeds(c)
        assert len(seeds) == 3
        actions = {a.partition(" ")[2] for a in action_set(GATE)}
        assert actions == {"login", "admin_panel"}          # 与规划器同一词表
        for steps in seeds:
            assert set(_ops_of(steps)) <= actions
            for s in steps:
                assert set(s) <= {"json", "header", "query", "form",
                                  "cookie", "extract"}     # C10 步 schema

    def test_seeds_are_legal_prefix_sequences(self, ir_path, tmp_path):
        """R8 合法性:每步的 requires 原子,其生产者必在此前的前缀里
        (或全图无生产者 = coverage-gap,同规划器的 NO_PLAN 容忍)。"""
        from stateforge.evaluation.experiment.state_ablation import (
            inject_random_seeds, requires_producers)
        ir_p, _ = ir_path
        c = tmp_path / "c"
        inject_random_seeds(ir_p, GATE, {}, c, budget=5, seed="legal")
        from stateforge.solving.state_seed_injector import synthesize_micro_model
        sstg = synthesize_micro_model(GATE).sstg
        _requires, producers = requires_producers(GATE)
        for steps in _sequence_seeds(c):
            seq = _ops_of(steps)
            actions = [f"POST {op}" for op in seq]
            for i, action in enumerate(actions):
                for _a, atom, ed in sstg.out_edges(action, data=True):
                    if ed.get("kind") != "requires":
                        continue
                    ps = producers.get(atom, [])
                    if ps and not (set(ps) & set(actions[:i])):
                        pytest.fail(f"非法前缀:{seq} 第{i}步 {action} 的 "
                                    f"{atom} 生产者不在前缀")

    def test_no_goal_value_binding_neutral_only(self, ir_path, tmp_path):
        """消融边界钉死:E-rand 不带规划器知识——无 goal 字面值绑定
        (role != "admin")、无 _JSON_FILL,非 op 槽位一律中性 "x"。"""
        from stateforge.evaluation.experiment.state_ablation import (
            inject_random_seeds)
        ir_p, _ = ir_path
        c = tmp_path / "c"
        inject_random_seeds(ir_p, GATE, {}, c, budget=5, seed="neutral")
        saw_login_value_slot = False
        for steps in _sequence_seeds(c):
            for s in steps:
                for k, v in s.get("json", {}).items():
                    if k != "op":
                        assert v == NEUTRAL
                        saw_login_value_slot = True
                    else:
                        assert v in ("login", "admin_panel")
        assert saw_login_value_slot               # 非空断言(防 vacuous)

    def test_deterministic_per_seed(self, ir_path, tmp_path):
        """同 seed 两次独立采样 → 逐字节相同种子集(seeded ordering)。"""
        from stateforge.evaluation.experiment.state_ablation import (
            inject_random_seeds)
        ir_p, _ = ir_path
        c1, c2 = tmp_path / "w1" / "c", tmp_path / "w2" / "c"
        n1 = inject_random_seeds(ir_p, GATE, {}, c1, budget=3, seed="det")
        n2 = inject_random_seeds(ir_p, GATE, {}, c2, budget=3, seed="det")
        assert n1 == n2 == 3
        assert _corpus_files(c1) == _corpus_files(c2)

    def test_seed_stream_varies_across_seeds(self, ir_path, tmp_path):
        """不同 seed → 采样流不同(非恒定序列;20 个 seed 至少 2 种首序列)。
        各 seed 独立 spool 父目录——内容哈希去重跨同 spool 生效,共享父目录
        会让小样本空间跨 seed 碰撞 DUPLICATE(与注入器同款落盘纪律)。"""
        from stateforge.evaluation.experiment.state_ablation import (
            inject_random_seeds)
        ir_p, _ = ir_path
        firsts = set()
        for k in range(20):
            c = tmp_path / f"w{k}" / "c"           # 独立 workdir = 独立 spool
            inject_random_seeds(ir_p, GATE, {}, c, budget=1, seed=k)
            seeds = _sequence_seeds(c)
            assert seeds
            firsts.add(tuple(_ops_of(seeds[0])))
        assert len(firsts) >= 2

    def test_budget_caps(self, ir_path, tmp_path):
        ir_p, _ = ir_path
        from stateforge.evaluation.experiment.state_ablation import (
            inject_random_seeds)
        assert inject_random_seeds(ir_p, GATE, {}, tmp_path / "c",
                                   budget=2, seed="b") == 2

    def test_zero_when_all_atoms_reached(self, ir_path, tmp_path):
        """R2-(a) 保留:全部原子已达 → 零注入(与规划器同语义)。"""
        from stateforge.evaluation.experiment.state_ablation import (
            inject_random_seeds)
        ir_p, ir = ir_path
        covered = [bid for bid, b in ir.blocks.items()
                   if b.file.endswith("t_state_gate.py")]
        assert inject_random_seeds(ir_p, GATE, {"covered_blocks": covered},
                                   tmp_path / "c", budget=3, seed="r") == 0
        assert not (tmp_path / "c").exists() or \
            _sequence_seeds(tmp_path / "c") == []   # 零写入(诚实缺省)


class TestRandSamplerAppModel:
    @staticmethod
    def _matches_route(path: str, routes: set[str]) -> bool:
        """渲染 path(/orders/?oid/pay)↔ 路由模式(/orders/<int:oid>/pay)。"""
        import re
        for r in routes:
            if re.fullmatch(re.sub(r"<[^>]+>", r"[^/]+", re.escape(r)), path):
                return True
        return False

    def test_seeds_are_request_dicts_on_extracted_routes(self, vs_ir, tmp_path):
        """AppModel 路径:随机步 = method/path 请求 dict,path ∈ 提出的路由
        (<slot> → ?slot 符号,靶侧绑定表运行期代入)。"""
        from stateforge.analysis.flask_adapter import extract_flask
        from stateforge.evaluation.experiment.state_ablation import (
            action_set, inject_random_seeds)
        vs_p, _ = vs_ir
        ft = VULNSHOP / "fuzz_target.py"
        c = tmp_path / "c"
        assert inject_random_seeds(vs_p, ft, {}, c, budget=3, seed="v1") >= 1
        routes = {ep.route for ep in extract_flask(VULNSHOP).endpoints}
        actions = action_set(ft)
        assert actions                              # 同一动作集非空
        for steps in _sequence_seeds(c):
            for s in steps:
                assert s.get("method")
                assert self._matches_route(s.get("path", ""), routes)
                for k, v in s.get("json", {}).items():
                    assert v == NEUTRAL             # 无规划器值绑定

    def test_deterministic_per_seed(self, vs_ir, tmp_path):
        from stateforge.evaluation.experiment.state_ablation import (
            inject_random_seeds)
        vs_p, _ = vs_ir
        ft = VULNSHOP / "fuzz_target.py"
        c1, c2 = tmp_path / "w1" / "c", tmp_path / "w2" / "c"
        n1 = inject_random_seeds(vs_p, ft, {}, c1, budget=3, seed="vd")
        n2 = inject_random_seeds(vs_p, ft, {}, c2, budget=3, seed="vd")
        assert n1 == n2 >= 1
        assert _corpus_files(c1) == _corpus_files(c2)


# ------------------------------------- E-always:R2-(b) 旁路(钉扎场景)


class TestAlwaysTriggerSeam:
    def _workdir(self, tmp_path, ir, target, covered) -> Path:
        wd = tmp_path / "wd"
        (wd / "epochs").mkdir(parents=True)
        (wd / "epochs" / "global_state.json").write_text(
            json.dumps({"blocks": covered, "covered_blocks": covered}),
            encoding="utf-8")
        (wd / "lf_corpus").mkdir()
        return wd

    def _audit(self, wd: Path) -> list[dict]:
        p = wd / "epochs" / "state_injections.jsonl"
        if not p.exists():
            return []
        return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]

    def test_r2_skips_on_progress_by_default(self, multi_atom, tmp_path):
        """钉扎场景:2/3 原子新判达(R2-b 命中)→ 缺省 trigger 不注入。"""
        from stateforge.evaluation.experiment.epoch import (
            _inject_state_seeds_between_epochs)
        from stateforge.solving.state_seed_injector import reached_state_atoms
        target, ir_p, ir = multi_atom
        reached = reached_state_atoms(
            ir, target, {"covered_blocks": _cover_blocks(
                ir, target, {"Session.role=admin", "Session.role=dev"})})
        assert reached == {"Session.role=admin", "Session.role=dev"}
        wd = self._workdir(tmp_path, ir, target, _cover_blocks(
            ir, target, {"Session.role=admin", "Session.role=dev"}))
        n = _inject_state_seeds_between_epochs(str(ir_p), wd, wd / "lf_corpus",
                                               str(target), epoch_num=2)
        assert n == 0
        assert any(r.get("status") == "SKIP_R2_PROGRESS" for r in self._audit(wd))

    def test_always_injects_where_r2_skips(self, multi_atom, tmp_path):
        """同场景 trigger="always":旁路 R2-(b),未达原子(guest)存在即注入。"""
        from stateforge.evaluation.experiment.epoch import (
            _inject_state_seeds_between_epochs)
        target, ir_p, ir = multi_atom
        wd = self._workdir(tmp_path, ir, target, _cover_blocks(
            ir, target, {"Session.role=admin", "Session.role=dev"}))
        n = _inject_state_seeds_between_epochs(str(ir_p), wd, wd / "lf_corpus",
                                               str(target), epoch_num=2,
                                               trigger="always")
        assert n >= 1
        statuses = [r.get("status") for r in self._audit(wd)]
        assert "INJECT_ALWAYS" in statuses
        assert "INJECTED" in statuses
        rec = next(r for r in self._audit(wd)
                   if r.get("status") == "INJECT_ALWAYS")
        assert rec["new_atoms"] == ["Session.role=admin", "Session.role=dev"]

    def test_always_respects_unreached_clause(self, multi_atom, tmp_path):
        """E-always 保留 R2-(a):全部原子已达 → 即使 trigger=always 也零注入。"""
        from stateforge.evaluation.experiment.epoch import (
            _inject_state_seeds_between_epochs)
        target, ir_p, ir = multi_atom
        wd = self._workdir(tmp_path, ir, target, _cover_blocks(
            ir, target, {"Session.role=admin", "Session.role=dev",
                         "Session.role=guest"}))
        n = _inject_state_seeds_between_epochs(str(ir_p), wd, wd / "lf_corpus",
                                               str(target), epoch_num=2,
                                               trigger="always")
        assert n == 0
        assert _sequence_seeds(wd / "lf_corpus") == []

    def test_custom_injector_receives_seam_contract(self, multi_atom, tmp_path):
        """injector 钩子:接缝以与 inject_state_seeds 同一契约调用并透传返回值。"""
        from stateforge.evaluation.experiment import epoch as epoch_mod
        from stateforge.solving.state_seed_injector import STATE_BUDGET
        target, ir_p, ir = multi_atom
        wd = self._workdir(tmp_path, ir, target, [])
        calls = []

        def fake(ir_obj, tgt, state, corpus_dir, budget=STATE_BUDGET):
            calls.append({"target": str(tgt), "budget": budget,
                          "corpus": str(corpus_dir)})
            return 7

        n = epoch_mod._inject_state_seeds_between_epochs(
            str(ir_p), wd, wd / "lf_corpus", str(target), epoch_num=2,
            injector=fake)
        assert n == 7
        assert len(calls) == 1
        assert calls[0]["budget"] == STATE_BUDGET
        assert calls[0]["target"] == str(target)

    def test_seam_params_default_gated(self):
        """门控契约:消融参数为带缺省值的可选参——缺省时 E 路径逐字节不变。"""
        from stateforge.evaluation.experiment.epoch import (
            _inject_state_seeds_between_epochs, run_epochs)
        sig = inspect.signature(_inject_state_seeds_between_epochs)
        assert sig.parameters["trigger"].default == "r2"
        assert sig.parameters["injector"].default is None
        sig2 = inspect.signature(run_epochs)
        assert sig2.parameters["state_trigger"].default == "r2"
        assert sig2.parameters["state_injector"].default is None

    def test_run_epochs_passthrough_state_injector(self, ir_path, tmp_path):
        """run_epochs → 接缝 → injector 钩子全链透传(policy=state 专用)。"""
        from stateforge.evaluation.experiment.epoch import run_epochs
        from stateforge.solving.state_seed_injector import STATE_BUDGET
        ir_p, _ = ir_path
        wd = tmp_path / "wd_pass"
        budgets = []

        def fake(ir_obj, tgt, state, corpus_dir, budget=STATE_BUDGET):
            budgets.append(budget)
            return 5

        res = run_epochs(str(GATE), str(ir_p), wd, seconds_per_epoch=1.0,
                         epochs=2, seed=7, group="P", mode="feedback",
                         use_feedback=True, policy="state",
                         state_injector=fake)
        assert budgets == [STATE_BUDGET]               # epoch2 边界恰一次
        assert res["state_injections_total"] == 5

    def test_run_epochs_passthrough_state_trigger_always(self, multi_atom,
                                                         tmp_path):
        """run_epochs(state_trigger="always"):真驱动 E2E 钉扎——预置语料让
        epoch1 真实判达 admin/dev(R2-b 会命中的进展),旁路后 epoch2 边界
        仍注入(guest 链);对照:同场景缺省 trigger 零注入。"""
        from stateforge.evaluation.experiment.epoch import run_epochs
        target, ir_p, _ = multi_atom
        audit_of = lambda wd: (wd / "epochs" / "state_injections.jsonl"
                               ).read_text() \
            if (wd / "epochs" / "state_injections.jsonl").exists() else ""

        def prep(wd: Path) -> Path:
            (wd / "lf_corpus").mkdir(parents=True)
            for role in ("admin", "dev"):          # epoch1 真实判达两原子
                steps = [{"json": {"op": "login", "role": role}},
                         {"json": {"op": role}}]
                (wd / "lf_corpus" / f"seed_{role}").write_bytes(
                    json.dumps({"steps": steps}).encode("utf-8"))
            return wd

        wd_a = prep(tmp_path / "wd_always")
        res = run_epochs(str(target), str(ir_p), wd_a, seconds_per_epoch=1.0,
                         epochs=2, seed=7, group="AL", mode="feedback",
                         use_feedback=True, policy="state",
                         state_trigger="always")
        assert "INJECT_ALWAYS" in audit_of(wd_a)
        assert res["state_injections_total"] >= 1

        wd_d = prep(tmp_path / "wd_default")
        res_d = run_epochs(str(target), str(ir_p), wd_d, seconds_per_epoch=1.0,
                           epochs=2, seed=7, group="AD", mode="feedback",
                           use_feedback=True, policy="state")
        assert "SKIP_R2_PROGRESS" in audit_of(wd_d)   # 唯一差异 = trigger 参数
        assert res_d["state_injections_total"] == 0


# ------------------------------------- run_ablation:三臂报告与配对差


class TestRunAblation:
    def test_report_shape_and_paired_diffs(self, ir_path, tmp_path):
        """R9:三臂同靶/同 seed/budget;报告含 state_arm_report 同款形状
        (blocks/edges + 状态三指标)+ E−E-rand / E−E-always 配对差。"""
        from stateforge.evaluation.experiment.state_ablation import run_ablation
        ir_p, _ = ir_path
        out = tmp_path / "ab"
        rep = run_ablation(str(GATE), str(ir_p), out, runs=1, epochs=2,
                           seconds=2.0)
        assert rep["target"] == str(GATE) and rep["ir"] == str(ir_p)
        assert set(rep["arms"]) == {"E", "E-rand", "E-always"}
        seeds = set()
        for arm, a in rep["arms"].items():
            assert a["mean_blocks"] is not None
            assert {"atoms_reached", "atoms", "transitions_observed",
                    "transitions", "deep_state_blocks", "deep_block_ids",
                    "deep_functions"} <= set(a["state"])
            assert a["state_runs"] and a["state_runs"][0]["run"] == "run00"
            row = a["per_run"]["run00"]
            seeds.add(row["seed"])
            assert {"blocks", "edges", "executions",
                    "injections_total"} <= set(row)
        assert len(seeds) == 1                        # 三臂同 seed
        # E 臂 = 未触碰的基线路径:其 run 目录无随机臂审计痕
        e_audit = (out / "run00" / "E" / "epochs"
                   / "state_injections.jsonl").read_text()
        assert '"path": "random"' not in e_audit
        rand_audit = (out / "run00" / "E-rand" / "epochs"
                      / "state_injections.jsonl").read_text()
        assert '"path": "random"' in rand_audit       # E-rand 走采样器
        pw = rep["pairwise"]
        assert {"E_minus_E-rand", "E_minus_E-always"} <= set(pw)
        for key, other in (("E_minus_E-rand", "E-rand"),
                           ("E_minus_E-always", "E-always")):
            assert {"blocks", "atoms"} <= set(pw[key])
            e = rep["arms"]["E"]["per_run"]["run00"]["blocks"]
            x = rep["arms"][other]["per_run"]["run00"]["blocks"]
            assert pw[key]["blocks"] == round(e - x, 1)

    def test_three_arms_same_trigger_semantics_budget(self, ir_path, tmp_path):
        """三臂 budget 同源(接缝 STATE_BUDGET):E-rand/E-always 审计的
        每边界写入数 ≤ STATE_BUDGET;E-always 的旁路痕在 run 目录可查。"""
        from stateforge.evaluation.experiment.state_ablation import run_ablation
        from stateforge.solving.state_seed_injector import STATE_BUDGET
        ir_p, _ = ir_path
        out = tmp_path / "ab2"
        run_ablation(str(GATE), str(ir_p), out, runs=1, epochs=2, seconds=1.0)
        for arm in ("E-rand", "E-always"):
            audit = [json.loads(x) for x in (out / "run00" / arm / "epochs"
                    / "state_injections.jsonl").read_text().splitlines()
                    if x.strip()]
            injected = [r for r in audit if r.get("status") == "INJECTED"]
            assert len(injected) <= STATE_BUDGET      # budget 封顶同源

    def test_report_discloses_atoms_measurability(self, ir_path, tmp_path):
        """I3(终审前向修):微靶报告落盘 state_atoms_measurable=True;
        无微模型靶(AppModel)为 False + 附注,render_text 三指标列
        渲染 "-"(未测),不显示为测得 0。"""
        from stateforge.evaluation.experiment.state_ablation import (
            render_text, run_ablation)
        ir_p, _ = ir_path
        rep = run_ablation(str(GATE), str(ir_p), tmp_path / "ab3",
                           runs=1, epochs=1, seconds=1.0)
        assert rep["state_atoms_measurable"] is True   # 微靶可测
        assert "state_atoms_note" not in rep
        # 无微模型靶:render_text 纯函数,直接喂合成报告形状
        fake = {"target": "vs.py", "runs": 1, "epochs": 1, "seconds": 1.0,
                "state_atoms_measurable": False,
                "state_atoms_note": "状态原子三指标仅微靶路径可测;本靶无微模型"
                                    "(synthesize_micro_model=None,AppModel 路径),"
                                    "atoms/transitions/deep_blocks 为未测,非测得零",
                "arms": {a: {"mean_blocks": 10, "mean_edges": 9,
                             "state": {"atoms_reached": 0,
                                       "transitions_observed": 0,
                                       "deep_state_blocks": 0},
                             "per_run": {"run00": {"injections_total": 1}}}
                         for a in ("E", "E-rand", "E-always")},
                "pairwise": {"E_minus_E-rand": {"blocks": 1.0, "atoms": 0.0},
                             "E_minus_E-always": {"blocks": 0.0, "atoms": 0.0}}}
        text = render_text(fake)
        assert "注: 状态原子三指标仅微靶路径可测" in text
        assert "n/a(未测:无微模型)" in text
        assert "atoms=0" not in text                    # 不显示测得 0
