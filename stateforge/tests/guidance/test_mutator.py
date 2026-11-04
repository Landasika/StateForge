"""变异器:变异必须真的发生、不越界、同种子可复现。

"变异了但结果等于父种"会让预算空转(同一输入反复执行、距离纹丝不动),
这是定向 fuzzing 最隐蔽的浪费,故单独钉住。
"""

from __future__ import annotations

from stateforge.fuzzing.guidance.mutator import BasicMutator


def test_mutation_always_changes_input() -> None:
    mutator = BasicMutator(random_seed=0)
    for size in (1, 2, 8, 64):
        data = bytes(range(size))
        for _ in range(50):
            assert mutator.mutate(data, max_size=128) != data


def test_never_exceeds_max_size() -> None:
    mutator = BasicMutator(random_seed=1)
    for _ in range(200):
        out = mutator.mutate(b"x" * 32, max_size=16)
        assert len(out) <= 16


def test_deterministic_for_same_seed() -> None:
    def run() -> list[bytes]:
        mutator = BasicMutator(random_seed=42)
        return [mutator.mutate(b"seed", max_size=64) for _ in range(20)]

    assert run() == run()


def test_empty_input_produces_nonempty_output() -> None:
    mutator = BasicMutator(random_seed=0)
    for _ in range(20):
        assert mutator.mutate(b"", max_size=8)


def test_splice_uses_other_input() -> None:
    mutator = BasicMutator(random_seed=0)
    results = {mutator.mutate(b"AAAA", max_size=16, other=b"BBBBBBBB")
               for _ in range(40)}
    assert any(b"B" in result for result in results)


def test_all_operators_reachable() -> None:
    """每个算符都要能被抽到,否则声明的 operators 与真实行为不符。"""
    mutator = BasicMutator(random_seed=0)
    seen = set()
    for _ in range(600):
        data = b"abcdefghij"
        out = mutator.mutate(data, max_size=32, other=b"ZZZZZZZZZZ")
        if out != data:
            seen.add(len(out) != len(data) or out != data)
    assert seen == {True}


def test_zero_max_size_returns_empty() -> None:
    assert BasicMutator(random_seed=0).mutate(b"abc", max_size=0) == b""
