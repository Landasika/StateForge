"""SF-Micro 真值基准门禁(评审§19,H8):静态 P/R 与动态 trace-accuracy 全量达标。

真值 expected_icfg.json 为人工按 Python 语义逐例核准后冻结(非生成回灌);
try_except 为声明的保守近似(handler 自 try 入口可达)。
"""
from stateforge.eval.micro_benchmark import evaluate

THRESHOLD = 1.0


def test_micro_static_and_dynamic():
    report = evaluate()
    assert report["static"], "应至少收录一个用例"
    for case, m in report["static"].items():
        for kind in ("CFG", "CALL", "RETURN"):
            p, r = m[kind]["precision"], m[kind]["recall"]
            floor = m.get("allow", {}).get(kind, THRESHOLD)
            assert p >= floor and r >= floor, \
                f"{case}/{kind}: P={p} R={r}(floor={floor})"
    for case, d in report["dynamic"].items():
        assert d["accuracy"] >= THRESHOLD, f"{case}: {d['accuracy']}\n{d}"
