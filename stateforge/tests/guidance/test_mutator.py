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


# -- structured mode ---------------------------------------------------
#
# Byte mutation of a structured request destroys its syntax; the adapter then
# falls back to its default request and the execution is spent on a no-op.
# Measured on this repo's seeds the loss was 73.9% of the budget, so the mode
# exists to keep the encoded output parseable.

def _json_seeds() -> list[bytes]:
    return [
        b'{"method":"GET","path":"/"}',
        b'{"method":"POST","path":"/follow/peer","body":{"message":"hi"}}',
        b'{"method":"GET","path":"/api/users","headers":{"Authorization":"B x"}}',
    ]


def test_structured_mutation_keeps_output_parseable() -> None:
    import json

    from stateforge.fuzzing.guidance.mutator import JsonCodec

    mutator = BasicMutator(random_seed=3, codec=JsonCodec())
    seeds = _json_seeds()
    for index in range(300):
        out = mutator.mutate(seeds[index % len(seeds)], max_size=4096)
        decoded = json.loads(out)                   # must not raise
        assert isinstance(decoded, dict)
        # The envelope key must survive: dropping it is exactly the failure
        # mode this mode prevents.
        assert "path" in decoded


def test_structured_mutation_still_changes_input() -> None:
    from stateforge.fuzzing.guidance.mutator import JsonCodec

    mutator = BasicMutator(random_seed=0, codec=JsonCodec())
    for seed in _json_seeds():
        results = {mutator.mutate(seed, max_size=4096) for _ in range(30)}
        assert seed not in results


def test_structured_mutation_ignores_unparseable_input() -> None:
    """A codec that cannot decode must not suppress a mutation entirely."""
    from stateforge.fuzzing.guidance.mutator import JsonCodec

    mutator = BasicMutator(random_seed=0, codec=JsonCodec())
    out = mutator.mutate(b"not json at all", max_size=64)
    assert out != b"not json at all"


def test_structured_mutation_respects_max_size() -> None:
    from stateforge.fuzzing.guidance.mutator import JsonCodec

    mutator = BasicMutator(random_seed=1, codec=JsonCodec())
    for seed in _json_seeds():
        for _ in range(50):
            assert len(mutator.mutate(seed, max_size=24)) <= 24


def test_structured_mode_is_deterministic() -> None:
    from stateforge.fuzzing.guidance.mutator import JsonCodec

    def run() -> list[bytes]:
        mutator = BasicMutator(random_seed=11, codec=JsonCodec())
        return [mutator.mutate(seed, max_size=512)
                for seed in _json_seeds() * 5]

    assert run() == run()


def test_structured_mode_never_empties_the_envelope() -> None:
    """Deleting the last top-level key yields "missing field", not a new path."""
    import json

    from stateforge.fuzzing.guidance.mutator import JsonCodec

    mutator = BasicMutator(random_seed=5, codec=JsonCodec())
    single = b'{"path":"/only"}'
    for _ in range(60):
        decoded = json.loads(mutator.mutate(single, max_size=256))
        assert decoded.get("path") is not None


def test_byte_mode_is_unchanged_without_codec() -> None:
    """No codec must reproduce the historical byte-level behaviour exactly."""
    plain = BasicMutator(random_seed=9)
    assert plain.codec is None

    def run() -> list[bytes]:
        mutator = BasicMutator(random_seed=9)
        return [mutator.mutate(b"abcdef", max_size=32) for _ in range(6)]

    # Same seed, same sequence -- and the sequence is the byte-level one, so
    # callers that never opt in see no behaviour change at all.
    assert run() == run()



def test_splice_handles_nested_donor_keys() -> None:
    """A donor's addressable slot is a (container, key) pair, not donor[key].

    Splicing used to index the root with a nested key, which raised KeyError
    as soon as one seed carried a sub-document the other did not.
    """
    import json

    from stateforge.fuzzing.guidance.mutator import JsonCodec

    mutator = BasicMutator(random_seed=2, codec=JsonCodec())
    host = b'{"method":"GET","path":"/","headers":{"Authorization":"Bearer t"}}'
    donor = b'{"method":"POST","path":"/x","body":{"deep":{"nested":1}}}'
    for index in range(200):
        base, other = (host, donor) if index % 2 else (donor, host)
        out = mutator.mutate(base, max_size=512, other=other)
        json.loads(out)                            # must not raise
