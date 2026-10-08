"""Phase-4.1B 检查点二验收:Level 4-9 全链 + BS-Micro 12 用例端到端。"""
import json
import textwrap
from pathlib import Path

import pytest

from stateforge.solving.branch_observer import BranchObservation
from stateforge.solving.branch_solver import solve
from stateforge.solving.input_provenance import resolve_operand_source
from stateforge.fuzzing.mutation_hint import MutationHint
from stateforge.solving.solve_loop import solve_loop
from stateforge.analysis.static.icfg import build_icfg

BS = Path(__file__).parents[2] / "fixtures" / "bs_micro"


# ---------------------------------------------------------------- Level 4

def _prov(src, guard_line=3):
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "app.py"
        p.write_text(textwrap.dedent(src), encoding="utf-8")
        return resolve_operand_source(p, "run", guard_line)


def test_level4_supported_cases_100pct():
    cases = [
        ('def run(request):\n    x = request.args["x"]\n    if x == "a":\n'
         '        return 1\n', "QUERY", "x", "identity"),
        ('def run(request):\n    x = int(request.args["x"])\n    if x == 1:\n'
         '        return 1\n', "QUERY", "x", "int"),
        ('def run(request):\n    x = request.form["n"]\n    if x == "a":\n'
         '        return 1\n', "FORM", "n", "identity"),
        ('def run(request):\n    x = int(request.form["n"])\n    if x == 1:\n'
         '        return 1\n', "FORM", "n", "int"),
        ('def run(request):\n    x = request.json["c"]\n    if x == 1:\n'
         '        return 1\n', "JSON", "c", "identity"),
        ('def run(request):\n    x = int(request.json["c"])\n    if x == 1:\n'
         '        return 1\n', "JSON", "c", "int"),
    ]
    for src, kind, key, transform in cases:
        s = _prov(src)
        assert (s.status, s.kind, s.key, s.transform) == \
            ("ok", kind, key, transform), src


def test_level4_refusals():
    assert _prov('def compute():\n    return 1\n\n\ndef run(request):\n'
                 '    x = compute()\n    if x == 1:\n        return 1\n',
                 guard_line=7).status == "SOURCE_UNKNOWN"
    assert _prov('def run(request):\n'
                 '    x = int(request.args["x"]) % 101\n    if x == 13:\n'
                 '        return 1\n').status == "UNRESOLVED_TRANSFORM"


# ---------------------------------------------------------------- Level 5

def _obs(op, lhs, rhs):
    return BranchObservation("g", op, lhs, rhs, lhs == rhs, 1)


def test_level5_frozen_rules():
    r = solve("f", "g", "true", "Eq", True, _obs("Eq", 20, 731), "QUERY", "x")
    assert r.reason == "solved" and r.target_value == 731
    r = solve("f", "g", "true", "Gt", True, _obs("Gt", 5, 100), "QUERY", "x")
    assert r.target_value == 101
    r = solve("f", "g", "true", "Lt", True, _obs("Lt", 20, 10), "QUERY", "x")
    assert r.target_value == 9
    r = solve("f", "g", "true", "GtE", True, _obs("GtE", 3, 10), "JSON", "c")
    assert r.target_value == 10
    # desired FALSE = 补谓词
    r = solve("f", "g", "false", "Eq", True, _obs("Eq", 5, 5), "QUERY", "x")
    assert r.target_value == 6                      # !(x==5) → x != 5 → C+1
    r = solve("f", "g", "false", "LtE", True, _obs("LtE", 12, 10),
              "QUERY", "x")
    assert r.target_value == 11                     # !(x<=10) → x > 10 → C+1
    r = solve("f", "g", "true", "Eq", True, _obs("Eq", "user1", "admin"),
              "QUERY", "n")
    assert r.target_value == "admin"


def test_level5_refusals():
    r = solve("f", "g", "true", "Eq", True, _obs("Eq", "20", 731),
              "QUERY", "x")
    assert r.reason == "NOT_SOLVABLE_TYPE_MISMATCH"
    r = solve("f", "g", "true", "Lt", True, _obs("Lt", "a", "b"), "QUERY", "x")
    assert r.reason == "NOT_SOLVABLE_STRING_ORDERED"


# ---------------------------------------------------------------- Level 6

def test_level6_field_isolation():
    from stateforge.fuzzing.constraint_mutator import (MutationError,
                                                    apply_hint)
    seed = {"query": {"x": "20", "item": "book", "mode": "fast"},
            "form": {"csrf": "tok"}, "json": {"n": 1}}
    hint = MutationHint("f", "g", "QUERY", "x", 20, 731, "Eq", "true",
                        "solved")
    out = apply_hint(seed, hint)
    assert out["query"]["x"] == "731"
    assert out["query"]["item"] == "book" and out["query"]["mode"] == "fast"
    assert out["form"] == {"csrf": "tok"} and out["json"] == {"n": 1}
    assert seed["query"]["x"] == "20"                # 原种子不被改
    # LLM 可创建新字段(不再 FIELD_NOT_FOUND)
    added = apply_hint(seed, MutationHint("f", "g", "QUERY", "absent", 1, 2,
                                          "Eq", "true", "solved"))
    assert added["query"]["absent"] == "2"  # 新字段被添加
    assert seed["query"].get("absent") is None  # 原种子不受影响
    json_hint = MutationHint("f", "g", "JSON", "n", 1, 10, "GtE", "true",
                             "solved")
    assert apply_hint(seed, json_hint)["json"]["n"] == 10   # JSON 保类型


# ---------------------------------------------------------------- Level 9

def _run_bs(case: str):
    d = BS / case
    ir = build_icfg(d)
    seed = json.loads((d / "input.json").read_text(encoding="utf-8"))
    return solve_loop(d / "app.py", ir, seed)


def _events(res, stage):
    return [e for e in res.events if e["stage"] == stage]


@pytest.mark.parametrize("case,hint_target,field", [
    ("BS01_query_int_eq", "731", "QUERY[x]"),
    ("BS02_query_int_gt", "101", "QUERY[x]"),
    ("BS03_query_false_edge", "6", "QUERY[x]"),
    ("BS04_query_string_eq", "admin", "QUERY[name]"),
    ("BS05_form_int_eq", "42", "FORM[count]"),
    ("BS06_json_int_ge", 10, "JSON[count]"),
    # P0-1:输入在比较符右侧(常量在左),canonicalize 后求解方向必须正确
    ("BS13_rhs_gt", "11", "QUERY[x]"),      # 10 < x → x > 10 → 11
    ("BS14_rhs_ge", "10", "QUERY[x]"),      # 10 <= x → x >= 10 → 10
    ("BS15_rhs_lt", "99", "QUERY[x]"),      # 100 > x → x < 100 → 99
    ("BS16_rhs_le", "100", "QUERY[x]"),     # 100 >= x → x <= 100 → 100
    # 4.1-A 增量:HEADER/COOKIE 源 + 变换链
    ("BS17_header_eq", "admin", "HEADER[X-Mode]"),
    ("BS18_cookie_eq", "s3cret", "COOKIE[tok]"),
    ("BS19_strip_lower_chain", "admin", "QUERY[name]"),   # strip->lower 链
    ("BS20_float_transform", "1.5", "QUERY[v]"),          # float 链,>= 1.5
])
def test_bs_single_frontier_crossed(case, hint_target, field):
    res = _run_bs(case)
    hints = _events(res, "HINT_GENERATED")
    assert hints and hints[0]["hint_reason"] == "solved"
    assert str(hints[0]["target"]) == str(hint_target)
    assert hints[0]["source"] == field
    assert _events(res, "CANDIDATE_GENERATED")
    assert _events(res, "BRANCH_SATISFIED")[0]["satisfied"] is True
    assert _events(res, "FRONTIER_CROSSED"), res.events
    assert res.target_reached


def test_rhs_solver_rules_frozen():
    # 评审示例:10 < x,desired TRUE → x=11(而非错误的 9)
    r = solve("f", "g", "true", "Lt", False, _obs("Lt", 10, 5), "QUERY", "x")
    assert r.reason == "solved" and r.target_value == 11


def test_bs07_nested_two_guards():
    res = _run_bs("BS07_nested_two_guards")
    hints = _events(res, "HINT_GENERATED")
    assert [str(h["target"]) for h in hints] == ["731", "18"]
    crossed = _events(res, "FRONTIER_CROSSED")
    assert len(crossed) == 2 and res.target_reached
    # 第二个候选只改 y,x 保持 731
    cands = _events(res, "CANDIDATE_GENERATED")
    assert cands[1]["candidate"]["query"]["x"] == "731"


def test_nested_seed_isolation():
    """P1-2:S0 恒不变;S1 仅改 x;S2 相对 S1 仅改 y。"""
    import copy
    d = BS / "BS07_nested_two_guards"
    s0 = json.loads((d / "input.json").read_text(encoding="utf-8"))
    s0_frozen = copy.deepcopy(s0)
    res = solve_loop(d / "app.py", build_icfg(d), s0)
    cands = [e["candidate"] for e in _events(res, "CANDIDATE_GENERATED")]
    s1, s2 = cands[0], cands[1]
    assert s0 == s0_frozen, "原始种子必须不可变"
    assert s1["query"]["x"] == "731" and s1["query"]["y"] == "5"
    assert s2["query"]["x"] == "731" and s2["query"]["y"] == "18"
    assert set(s1) == set(s0) and set(s2) == set(s0)


def test_bs08_already_satisfied():
    res = _run_bs("BS08_already_satisfied")
    assert res.target_reached and not _events(res, "HINT_GENERATED")


@pytest.mark.parametrize("case,reason", [
    ("BS09_unknown_source", "NOT_SINGLE_INPUT_SOLVABLE"),
    ("BS10_unresolved_transform", "NOT_SINGLE_INPUT_SOLVABLE"),
    ("BS11_type_mismatch", "NOT_SOLVABLE_TYPE_MISMATCH"),
    ("BS12_non_input_guard", "NOT_SINGLE_INPUT_SOLVABLE"),
    ("BS21_values_ambiguous", "NOT_SINGLE_INPUT_SOLVABLE"),
    ("BS22_unsupported_chain", "NOT_SINGLE_INPUT_SOLVABLE"),
])
def test_bs_refusals(case, reason):
    res = _run_bs(case)
    hints = _events(res, "HINT_GENERATED")
    assert hints and hints[0]["hint_reason"] == reason, res.events
    assert not res.target_reached and not _events(res, "FRONTIER_CROSSED")


def test_micro_report_all_green():
    """SF-BranchSolveMicro 全 22 用例:overall=1.0、零失败。"""
    from stateforge.evaluation.branchsolve_report import run_report
    report = run_report()
    assert report["failures"] == []
    assert report["metrics"]["overall_accuracy"] == 1.0
    assert report["metrics"]["cross_case_frontier_crossing_rate"] == 1.0
    assert report["metrics"]["refuse_case_honesty_rate"] == 1.0
