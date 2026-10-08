"""P4-T3 vulnshop 状态化适配(AppModel 真路径,R7 双路径)——TDD 先行。

验收口径(任务简报 + R7 裁决):
- 分派按**靶可用性**:有 AppModel(分析器能从靶目录提出 Flask 端点)→
  AppModel 路径(extract_flask→build_sstg→plan_backward);否则微靶
  M1/M2 合成路径,行为逐字节不变(既有 test_state_seed_injector.py 全绿);
- goal 派生诚实:requires 原子 − 已达原子;「已达」= **写点覆盖代理**
  (见 vulnshop_state_bridge 模块 docstring:值匹配锚点优先,值盲锚点
  仅限 mutating 端点),语义钉死在测试里(哪个写点算/哪个不算);
- 渲染步 = 合法 C10 序列种子步(dict;method/path/json/header/extract),
  ?槽保持符号(靶侧运行期绑定),同输入同字节;
- E2E:真注入器 + 真驱动(entry_driver 子进程),注入的序列种子在真实
  Flask 栈上执行,register→login→create(→pay)链的效果以覆盖/账本观测;
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

VULNSHOP = Path(__file__).resolve().parents[2] / "targets" / "vulnshop"
FUZZ_TARGET = VULNSHOP / "fuzz_target.py"
STATEFUL = Path(__file__).resolve().parents[2] / "targets" / "stateful"
GATE = STATEFUL / "t_state_gate.py"

# vulnshop AppModel 的 requires 原子词表(锚点语义测试据此取块)
EXPECTED_ATOMS = {"Coupon.used=True", "Order.status=PAID",
                  "Session.authed_as=<authed>", "User.role=admin"}


@pytest.fixture(scope="module")
def vs_ir(tmp_path_factory) -> tuple[Path, object]:
    ir = build_icfg(VULNSHOP)
    p = tmp_path_factory.mktemp("t3vir") / "ir.json"
    ir.to_json(p)
    return p, ir


def _blocks_with_line(ir, line: int, fname: str = "app.py") -> list[str]:
    return [bid for bid, b in ir.blocks.items()
            if b.file.endswith(fname) and line in (b.line_numbers or [])]


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


def _routes_of(steps: list) -> list[tuple[str, str]]:
    return [(s.get("method"), s.get("path")) for s in steps]


def _load(path: Path, tag: str):
    """entry_driver 同款机制加载靶模块(独立 DB/绑定 = 干净先验态)。"""
    spec = importlib.util.spec_from_file_location(f"sf_t3v_{tag}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------ R7 双路径分派


class TestDispatch:
    def test_vulnshop_has_appmodel_micro_does_not(self):
        """分派按可用性:vulnshop 靶目录提出 11 端点;微靶目录零端点。"""
        from stateforge.solving.vulnshop_state_bridge import appmodel_path
        amp = appmodel_path(FUZZ_TARGET)
        assert amp is not None
        assert len(amp.model.endpoints) == 11
        assert {"POST /auth/login", "POST /orders/<int:oid>/pay"} <= \
            set(amp.sstg.nodes)
        assert appmodel_path(GATE) is None
        assert appmodel_path(STATEFUL / "t_state_shop.py") is None

    def test_goal_atoms_are_appmodel_requires_atoms(self):
        """goal 词表 = SSTG requires 原子(AppModel 词汇,非 M1 session 词)。"""
        from stateforge.solving.vulnshop_state_bridge import appmodel_path
        amp = appmodel_path(FUZZ_TARGET)
        assert set(amp.atoms) == EXPECTED_ATOMS

    def test_reached_dispatches_to_appmodel_vocabulary(self, vs_ir):
        """空覆盖 → 零已达;且词表来自 AppModel(分派真的换了路径)。"""
        from stateforge.solving.state_seed_injector import reached_state_atoms
        _, ir = vs_ir
        assert reached_state_atoms(ir, FUZZ_TARGET, {}) == set()

    def test_micro_path_still_injects_for_gate(self, vs_ir, tmp_path):
        """微路径非回归:同函数族对 t_state_gate 仍产 1 条 login→admin 种子。"""
        from stateforge.solving.state_seed_injector import inject_state_seeds
        _, _ = vs_ir
        gate_ir = build_icfg(STATEFUL)
        c = tmp_path / "corp"
        assert inject_state_seeds(gate_ir, GATE, {}, c) == 1
        (steps,) = _sequence_seeds(c)
        assert [s["json"]["op"] for s in steps] == ["login", "admin_panel"]


# ------------------------------------------------- 判达语义(写点覆盖代理)


class TestReachedSemantics:
    """「原子已达」= 该原子的**写点**代码已覆盖(生产侧代理,镜像微路径的
    守卫真分支消费侧代理)。值匹配锚点优先;仅当无值匹配锚点才用值盲锚点
    (mutating 端点内提及该属性的写语句)——两类锚点的边界在这里钉死。"""

    def _reached_with_lines(self, vs_ir, lines: list[int]) -> set[str]:
        from stateforge.solving.state_seed_injector import reached_state_atoms
        _, ir = vs_ir
        covered = {bid for line in lines for bid in _blocks_with_line(ir, line)}
        assert covered, f"锚点行 {lines} 不在任何 IR 块内(锚点算错或 IR 变了)"
        return reached_state_atoms(ir, FUZZ_TARGET,
                                   {"covered_blocks": sorted(covered)})

    def test_PAID_write_assign_marks_PAID(self, vs_ir):
        """pay 的 `status = "PAID"` 赋值(256,值匹配锚点)→ PAID 判达。"""
        assert "Order.status=PAID" in self._reached_with_lines(vs_ir, [256])

    def test_create_insert_alone_does_not_mark_PAID(self, vs_ir):
        """诚实边界:create 的 INSERT(208,写的是 CREATED)不算 PAID——
        值匹配锚点非空时值盲锚点(如 pay 的 `status=?` SQL)不参与。"""
        got = self._reached_with_lines(vs_ir, [208])
        assert "Order.status=PAID" not in got
        assert "Order.status=SHIPPED" not in got     # 写 SHIPPED 的也不在词表

    def test_pay_entry_alone_does_not_mark_PAID(self, vs_ir):
        """函数入口(pay 239)覆盖 ≠ 状态写入(401/404 早退也覆盖入口)。"""
        assert "Order.status=PAID" not in self._reached_with_lines(vs_ir, [239])

    def test_role_update_marks_admin(self, vs_ir):
        """set_role 的 `UPDATE users SET role=?`(315,值盲锚点——静态无
        'admin' 字面写)→ User.role=admin 判达。"""
        assert "User.role=admin" in self._reached_with_lines(vs_ir, [315])

    def test_coupon_update_marks_used(self, vs_ir):
        """create 的 `UPDATE coupons SET used=1`(202,值盲锚点)→ 判达。"""
        assert "Coupon.used=True" in self._reached_with_lines(vs_ir, [202])

    def test_login_entry_marks_authed(self, vs_ir):
        """authed 原子锚点 = login 端点行(153;模型 Effect 锚点)——
        过近似:401 失败路径也覆盖(文档化边界)。"""
        assert "Session.authed_as=<authed>" in self._reached_with_lines(
            vs_ir, [153])

    def test_all_atoms_reached_disables_injection(self, vs_ir, tmp_path):
        """R2-a 镜像:全部词表原子判达 → 零注入。"""
        from stateforge.solving.state_seed_injector import inject_state_seeds
        _, ir = vs_ir
        covered = {bid for bid, b in ir.blocks.items()
                   if b.file.endswith("app.py")}
        assert inject_state_seeds(ir, FUZZ_TARGET,
                                  {"covered_blocks": sorted(covered)},
                                  tmp_path / "corp") == 0


# --------------------------------------------------------- 渲染(C10 步形状)


class TestRendering:
    def _inject(self, vs_ir, tmp_path, covered_blocks=None) -> Path:
        from stateforge.solving.state_seed_injector import inject_state_seeds
        _, ir = vs_ir
        corpus = tmp_path / "corp"
        state = {} if covered_blocks is None \
            else {"covered_blocks": covered_blocks}
        n = inject_state_seeds(ir, FUZZ_TARGET, state, corpus)
        assert n >= 1
        return corpus

    def test_steps_are_valid_c10_request_dicts(self, vs_ir, tmp_path):
        """每步 = dict,method ∈ 动词表、path 以 / 开头,键集有界。"""
        corpus = self._inject(vs_ir, tmp_path)
        seeds = _sequence_seeds(corpus)
        assert seeds
        for steps in seeds:
            for s in steps:
                assert isinstance(s, dict)
                assert s.get("method") in {"GET", "POST", "PUT", "DELETE",
                                           "PATCH"}
                assert isinstance(s.get("path"), str) and s["path"].startswith("/")
                assert set(s) <= {"method", "path", "json", "query", "form",
                                  "header", "cookie", "extract"}

    def test_chain_contains_register_login_create(self, vs_ir, tmp_path):
        """真路由链:register→login→create(认证链 + 资源生产)在种子里。"""
        corpus = self._inject(vs_ir, tmp_path)
        routes = [_routes_of(s) for s in _sequence_seeds(corpus)]
        head = [("POST", "/auth/register"), ("POST", "/auth/login"),
                ("POST", "/orders")]
        assert any(r[:3] == head for r in routes), routes

    def test_slots_stay_symbolic_with_extract(self, vs_ir, tmp_path):
        """?槽保持符号(靶侧运行期绑定):header 带 `Bearer ?token`,
        login 步 extract 回填 ?token,create 步 extract 回填 ?oid。"""
        corpus = self._inject(vs_ir, tmp_path)
        seeds = _sequence_seeds(corpus)
        assert any(s.get("header", {}).get("Authorization") == "Bearer ?token"
                   for steps in seeds for s in steps)
        assert any(s.get("extract", {}).get("token") == "?token"
                   for steps in seeds for s in steps)
        assert any(s.get("extract", {}).get("id") == "?oid"
                   for steps in seeds for s in steps)

    def test_unbound_path_slot_gets_extract_completion(self, vs_ir, tmp_path):
        """桥级完备化:?uid 无上游 extract → register 步(第一个 observes
        id 的步)补 extract id→?uid,链可执行而不是 404。"""
        corpus = self._inject(vs_ir, tmp_path)
        seeds = _sequence_seeds(corpus)
        admin = [s for s in seeds
                 if any("/admin/users/" in str(t.get("path")) for t in s)]
        assert admin, "User.role=admin goal 未产种子"
        assert any(t.get("path", "").endswith("/admin/users/?uid/role")
                   for t in admin[0])
        assert any(t.get("extract", {}).get("id") == "?uid" for t in admin[0])

    def test_determinism_and_dedup(self, vs_ir, tmp_path):
        """同输入同种子字节;同目录二次注入内容哈希去重为 0。"""
        from stateforge.solving.state_seed_injector import inject_state_seeds
        _, ir = vs_ir
        c1 = tmp_path / "w1" / "c"
        c2 = tmp_path / "w2" / "c"
        n1 = inject_state_seeds(ir, FUZZ_TARGET, {}, c1)
        n2 = inject_state_seeds(ir, FUZZ_TARGET, {}, c2)
        assert n1 == n2 >= 1
        assert _corpus_files(c1) == _corpus_files(c2)
        assert inject_state_seeds(ir, FUZZ_TARGET, {}, c1) == 0

    def test_budget_caps_written_seeds(self, vs_ir, tmp_path):
        from stateforge.solving.state_seed_injector import inject_state_seeds
        _, ir = vs_ir
        c = tmp_path / "corp"
        assert inject_state_seeds(ir, FUZZ_TARGET, {}, c, budget=1) == 1
        assert len(_sequence_seeds(c)) == 1

    def test_no_plan_atom_audited_not_counted(self, vs_ir, tmp_path):
        """Coupon.used=True 无生产者也无 soft 生产者 → NO_PLAN 审计,
        不占写入预算(budget=1 时仍应写出 PAID 种子)。"""
        from stateforge.solving.state_seed_injector import inject_state_seeds
        _, ir = vs_ir
        c = tmp_path / "corp"
        assert inject_state_seeds(ir, FUZZ_TARGET, {}, c, budget=1) == 1
        audit = [json.loads(x) for x in
                 (c.parent / "epochs" / "state_injections.jsonl")
                 .read_text(encoding="utf-8").splitlines() if x.strip()]
        assert any(r.get("status") == "NO_PLAN"
                   and r.get("goal") == "Coupon.used=True" for r in audit)
        assert any(r.get("status") == "INJECTED"
                   and r.get("goal") == "Order.status=PAID" for r in audit)


# --------------------------------------------- 靶侧消费(请求 dict 面,直跑)


class TestTargetSide:
    def test_seed_executes_real_route_chain(self, vs_ir, tmp_path):
        """注入种子经真实 dispatch_seed 打在真 Flask 栈上:
        register→login→create(→pay)全链走通——?token/?oid 运行期绑定,
        链效果可观测(pay 后订单状态 = PAID)。"""
        from stateforge.solving.state_seed_injector import inject_state_seeds
        _, ir = vs_ir
        c = tmp_path / "corp"
        inject_state_seeds(ir, FUZZ_TARGET, {}, c)
        paid = [d for d in _corpus_files(c).values()
                if decode_sequence_seed(d) is not None
                and any("/pay" in str(s.get("path")) for s in
                        decode_sequence_seed(d))]
        assert paid, "无含 pay 步的种子"
        mod = _load(FUZZ_TARGET, "chain")
        dispatch_seed(mod.target, paid[0])
        assert mod._BINDINGS.get("token")
        oid = mod._BINDINGS.get("oid")
        assert oid
        with mod._APP.test_request_context(
                f"/orders/{oid}", method="GET",
                headers={"Authorization":
                         f"Bearer {mod._BINDINGS['token']}"}):
            resp = mod._APP.full_dispatch_request()
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "PAID"

    def test_legacy_bytes_face_stays_unauthenticated(self, vs_ir, tmp_path):
        """legacy 字节面无免费认证:create 单步字节 → 401(深状态对盲变异
        关闭,状态链种子才是解锁通道——臂对照的前提)。"""
        mod = _load(FUZZ_TARGET, "legacy")
        mod.target(b"\x01\x02")           # FlaskAdapter 字节驱动(路由枚举)
        with mod._APP.test_request_context(
                "/orders", method="POST",
                json={"items": [{"sku": "book", "price": 1, "qty": 1}]}):
            resp = mod._APP.full_dispatch_request()
        assert resp.status_code == 401


# ------------------------------------------------- epoch 轮间接缝(R2 镜像)


class TestEpochSeamVulnshop:
    def _workdir(self, tmp_path, covered_blocks) -> Path:
        wd = tmp_path / "wd"
        (wd / "epochs").mkdir(parents=True)
        (wd / "epochs" / "global_state.json").write_text(
            json.dumps({"blocks": covered_blocks,
                        "covered_blocks": covered_blocks}),
            encoding="utf-8")
        (wd / "lf_corpus").mkdir()
        return wd

    def test_r2_progress_skips_injection(self, vs_ir, tmp_path):
        """上轮有新状态节点(pay 写点已覆盖)→ 跳过注入 + 账本记录原子。"""
        from stateforge.evaluation.experiment.epoch import (
            _inject_state_seeds_between_epochs)
        ir_p, ir = vs_ir
        covered = sorted(_blocks_with_line(ir, 256))    # PAID 写点
        wd = self._workdir(tmp_path, covered)
        assert _inject_state_seeds_between_epochs(str(ir_p), wd,
                                                  wd / "lf_corpus",
                                                  str(FUZZ_TARGET),
                                                  epoch_num=2) == 0
        ledger = json.loads(
            (wd / "epochs" / "state_ledger.json").read_text())
        assert "Order.status=PAID" in ledger["atoms"]
        assert "SKIP_R2_PROGRESS" in (wd / "epochs" /
                                      "state_injections.jsonl").read_text()

    def test_r2_stall_injects_real_route_chains(self, vs_ir, tmp_path):
        """无进展且存在未达原子 → 注入;种子含真路由链。"""
        from stateforge.evaluation.experiment.epoch import (
            _inject_state_seeds_between_epochs)
        ir_p, _ = vs_ir
        wd = self._workdir(tmp_path, [])
        n = _inject_state_seeds_between_epochs(str(ir_p), wd,
                                               wd / "lf_corpus",
                                               str(FUZZ_TARGET), epoch_num=2)
        assert n >= 1
        seeds = _sequence_seeds(wd / "lf_corpus")
        assert seeds
        assert any(r[0] == ("POST", "/auth/register")
                   for r in (_routes_of(s) for s in seeds))


# ----------------------------------------------------- E2E:真驱动小预算


class TestE2EVulnshop:
    def test_policy_state_unlocks_deep_state_through_driver(self, vs_ir,
                                                            tmp_path):
        """旗舰 E2E:vulnshop + policy=state、无初始序列种子 → 轮间注入器
        派生 register→login→create(→pay)真路由链落盘 → 子进程驱动真实
        执行 → 深状态写点入覆盖(原子账本增长)。"""
        from stateforge.evaluation.experiment.epoch import run_epochs
        ir_p, ir = vs_ir
        wd = tmp_path / "wd_state"
        result = run_epochs(str(FUZZ_TARGET), str(ir_p), wd,
                            seconds_per_epoch=6.0, epochs=3, seed=7,
                            group="T3V", mode="feedback", use_feedback=True,
                            policy="state")
        assert len(result["epoch_records"]) == 3

        audit = [json.loads(x) for x in
                 (wd / "epochs" / "state_injections.jsonl").read_text()
                 .splitlines() if x.strip()]
        injected = [r for r in audit if r.get("status") == "INJECTED"]
        assert injected and all(r["goal"] in EXPECTED_ATOMS for r in injected)
        assert any(any("/auth/register" in s for s in r["steps"])
                   and any("/auth/login" in s for s in r["steps"])
                   for r in injected)

        seeds = _sequence_seeds(wd / "lf_corpus")
        assert seeds
        assert any(r[:3] == [("POST", "/auth/register"),
                             ("POST", "/auth/login"),
                             ("POST", "/orders")]
                   for r in (_routes_of(s) for s in seeds))

        # 深链效果观测:pay 的 PAID 写点块入全局覆盖 ⇒ 原子账本记 PAID
        state = json.loads(
            (wd / "epochs" / "global_state.json").read_text(encoding="utf-8"))
        covered = set(state.get("covered_blocks") or state.get("blocks") or [])
        PAID_blocks = set(_blocks_with_line(ir, 256)) | \
            set(_blocks_with_line(ir, 254))
        assert covered & PAID_blocks, "pay 的 PAID 写点未覆盖:链未真实执行"
        assert "Order.status=PAID" in (result.get("state_atoms") or [])
