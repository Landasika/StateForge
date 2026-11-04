"""在线语料库:保留判据、容量裁剪、目标归档与可复现性。

裁剪是"定向灰盒会不会把好种子丢掉"的关键面,故重点断言:
已到目标的种子裁掉后必进归档(证据不可丢),且裁剪结果对同序列确定。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from stateforge.fuzzing.feedback.corpus import (
    GuidedCorpus,
    GuidedSeed,
    evaluate_seed,
    seed_id_for,
)
from stateforge.runtime.mapper import MappedTrace
from stateforge.tests.guidance.helpers import build_target_ir, target_block_id


@dataclass
class FakeResult:
    covered_blocks: frozenset[str] = field(default_factory=frozenset)
    covered_edges: frozenset[tuple[str, str]] = field(default_factory=frozenset)
    distance: int | None = 10
    target_reached: bool = False


def test_seed_id_is_content_addressed() -> None:
    assert seed_id_for(b"abc") == seed_id_for(b"abc")
    assert seed_id_for(b"abc") != seed_id_for(b"abd")
    assert seed_id_for(b"abc").startswith("seed_")


def test_consider_retains_novel_coverage() -> None:
    corpus = GuidedCorpus()
    assert corpus.consider(b"a", FakeResult(covered_blocks=frozenset({"x"}))) is True
    # 同输入不重复入库
    assert corpus.consider(b"a", FakeResult(covered_blocks=frozenset({"x"}))) is False
    # 无新块、无新边、距离未改善、已是同距离代表种 → 不收
    assert corpus.consider(b"b", FakeResult(covered_blocks=frozenset({"x"}))) is False


def test_new_distance_representative_is_kept() -> None:
    """同覆盖但距离是新的代表值 → 保留(距离维度本身也是新颖性)。"""
    corpus = GuidedCorpus()
    corpus.consider(b"a", FakeResult(covered_blocks=frozenset({"x"}), distance=10))
    assert corpus.consider(
        b"b", FakeResult(covered_blocks=frozenset({"x"}), distance=9)) is True


def test_first_seed_always_retained() -> None:
    corpus = GuidedCorpus()
    assert corpus.consider(b"first", FakeResult(distance=None)) is True


def test_target_reached_updates_state() -> None:
    corpus = GuidedCorpus()
    corpus.consider(b"hit", FakeResult(distance=0, target_reached=True))
    assert corpus.target_reached is True
    assert corpus.best_distance == 0


def test_target_seed_survives_pruning() -> None:
    """目标种子是强制代表:容量再小也要活下来,不被广度种子挤掉。"""
    corpus = GuidedCorpus(max_size=2)
    corpus.consider(b"hit", FakeResult(distance=0, target_reached=True))
    for index in range(6):
        corpus.consider(
            f"wide{index}".encode(),
            FakeResult(covered_blocks=frozenset({f"b{index}", f"c{index}"}),
                       covered_edges=frozenset({(f"b{index}", f"c{index}")}),
                       distance=5 + index),
        )
    assert len(corpus) <= 2
    assert b"hit" in [seed.data for seed in corpus.seeds]


def test_pruned_target_moves_to_archive() -> None:
    """目标种子多到必须裁掉时,被裁的那颗进归档而不是消失(证据不可丢)。"""
    corpus = GuidedCorpus(max_size=1)
    corpus.consider(b"hit1", FakeResult(covered_blocks=frozenset({"p"}),
                                        distance=0, target_reached=True))
    corpus.consider(b"hit2", FakeResult(covered_blocks=frozenset({"q"}),
                                        distance=0, target_reached=True))
    assert len(corpus) == 1
    archived = [seed.data for seed in corpus.archived_targets]
    live = [seed.data for seed in corpus.seeds]
    assert len(archived) == 1
    assert set(archived) | set(live) == {b"hit1", b"hit2"}
    # 归档与在世两处不重复计入同一颗种子
    assert not (set(archived) & set(live))


def test_best_seed_picks_nearest() -> None:
    corpus = GuidedCorpus()
    corpus._seeds["a"] = GuidedSeed("a", b"a", 5, 0.1, frozenset(), frozenset())
    corpus._seeds["b"] = GuidedSeed("b", b"b", 2, 0.3, frozenset(), frozenset())
    corpus._seeds["c"] = GuidedSeed("c", b"c", None, 0.0, frozenset(), frozenset())
    assert corpus.best_seed().seed_id == "b"


def test_best_seed_none_when_all_unknown() -> None:
    corpus = GuidedCorpus()
    corpus._seeds["c"] = GuidedSeed("c", b"c", None, 0.0, frozenset(), frozenset())
    assert corpus.best_seed() is None


def test_pruning_is_deterministic() -> None:
    def build() -> list[str]:
        corpus = GuidedCorpus(max_size=3)
        for index in range(12):
            corpus.consider(
                f"seed{index}".encode(),
                FakeResult(covered_blocks=frozenset({f"b{index % 5}"}),
                           covered_edges=frozenset({(f"b{index % 5}", f"e{index}")}),
                           distance=index % 4),
            )
        return [seed.seed_id for seed in corpus.seeds]

    assert build() == build()


def test_max_size_must_be_positive() -> None:
    with pytest.raises(ValueError):
        GuidedCorpus(max_size=0)


def test_evaluate_seed_without_targets() -> None:
    """无目标时既有口径不变(结构分 + 无最近距离)。"""
    ir = build_target_ir()
    trace = MappedTrace(blocks=[target_block_id(ir)])
    entry = evaluate_seed(ir, "s", trace, set())
    assert entry.nearest_target is None
    assert 0.0 <= entry.score <= 1.0


def test_evaluate_seed_with_targets_reports_nearest() -> None:
    ir = build_target_ir()
    target = target_block_id(ir)
    trace = MappedTrace(blocks=[target])
    entry = evaluate_seed(ir, "s", trace, {target})
    assert entry.nearest_target == 0
    assert entry.score == 1.0
