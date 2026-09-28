"""Phase-4.1 Level 2/3 验收:运行时观测 + 门内梯度距离。"""
from stateforge.feasibility.branch_distance import branch_distance
from stateforge.feasibility.branch_observer import load_observed_target
import textwrap

MICRO_EQ = textwrap.dedent('''
    def run(x):
        if x == 37:
            return prize()
        return 0

    def prize():
        return "hit"
''')


def test_level2_operand_observation():
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.py"
        p.write_text(MICRO_EQ, encoding="utf-8")
        mod, obs = load_observed_target(p)
        assert mod.run(12) == 0                      # 语义未变
        rec = obs.records[-1]
        assert (rec.lhs, rec.rhs, rec.actual) == (12, 37, False)
        assert rec.op == "Eq" and rec.execution_index >= 1


def test_level2_loop_multi_execution_and_truthy():
    import tempfile
    from pathlib import Path
    src = textwrap.dedent('''
        def run(items, flag):
            total = 0
            for it in items:
                if it > 2:
                    total += it
            if not flag:
                total = -1
            return total
    ''')
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.py"
        p.write_text(src, encoding="utf-8")
        mod, obs = load_observed_target(p)
        assert mod.run([1, 3, 5], True) == 8         # 语义保持
        cmps = [r for r in obs.records if r.op == "Gt"]
        assert [(r.lhs, r.rhs) for r in cmps] == [(1, 2), (3, 2), (5, 2)]
        nots = [r for r in obs.records if r.op == "not_truthy"]
        assert nots[0].lhs is True and nots[0].actual is False


def test_level2_unsupported_left_untouched():
    import tempfile
    from pathlib import Path
    src = textwrap.dedent('''
        def run(a, b):
            if 0 < a < 100 and b:
                return 1
            return 0
    ''')
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.py"
        p.write_text(src, encoding="utf-8")
        mod, obs = load_observed_target(p)
        assert mod.run(50, True) == 1
        assert obs.records == []                     # 无观测=OPERAND_UNAVAILABLE


def test_level3_gradient_to_gate():
    """顾问指定验收:27→17→7→1→0 式门内梯度(x==731)。"""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "g.py"
        p.write_text(textwrap.dedent('''
            def run(x):
                if x == 731:
                    return prize()
                return 0

            def prize():
                return "hit"
        '''), encoding="utf-8")
        mod, obs = load_observed_target(p)
        dists = []
        for x in (20, 600, 710, 730, 731):
            obs.records.clear()
            mod.run(x)
            d = branch_distance(obs.records[-1], "true")
            dists.append(int(d.raw_distance))
            assert d.satisfied == (x == 731)
        assert dists == [711, 131, 21, 1, 0], dists


def test_level3_desired_false_flips_predicate():
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "f.py"
        p.write_text("def run(x):\n    if x >= 10:\n        return 1\n    return 0\n",
                     encoding="utf-8")
        mod, obs = load_observed_target(p)
        obs.records.clear(); mod.run(3)
        d_false = branch_distance(obs.records[-1], "false")   # 想走 false 分支
        assert d_false.raw_distance == 0                       # x=3 已满足 false
        obs.records.clear(); mod.run(12)
        d_false2 = branch_distance(obs.records[-1], "false")
        assert d_false2.raw_distance == max(0, 12 - 10 + 1)    # 翻成 < 需 ≤9:距离 3
        assert branch_distance(obs.records[-1], "true").raw_distance == 0
