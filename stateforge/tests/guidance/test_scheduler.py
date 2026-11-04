"""调度器:选种概率、算力分配、epsilon 探索与可复现性。

关键不变量:(1) 概率和为 1;(2) 到目标/更近的种子权重更高;
(3) 同种子同序列;(4) 未知距离的种子不被永久饿死(epsilon 兜底)。
"""

from __future__ import annotations

import pytest

from stateforge.fuzzing.feedback.corpus import GuidedCorpus, GuidedSeed
from stateforge.fuzzing.guidance.scheduler import (
    EmptyCorpusError,
    GuidedScheduler,
    SchedulerError,
    UniformScheduler,
)


def make_seed(seed_id: str, distance: int | None, *,
              reached: bool = False, blocks: int = 1) -> GuidedSeed:
    return GuidedSeed(
        seed_id=seed_id,
        data=seed_id.encode(),
        distance=distance,
        score=0.0 if distance is None else 1.0 / (1.0 + distance),
        covered_blocks=frozenset(f"{seed_id}_b{i}" for i in range(blocks)),
        covered_edges=frozenset(),
        target_reached=reached,
    )


def make_corpus(*seeds: GuidedSeed, max_size: int = 100) -> GuidedCorpus:
    corpus = GuidedCorpus(max_size=max_size)
    for seed in seeds:
        corpus._seeds[seed.seed_id] = seed
    return corpus


def test_probabilities_sum_to_one() -> None:
    corpus = make_corpus(make_seed("a", 1), make_seed("b", 5), make_seed("c", 20))
    probabilities = GuidedScheduler().selection_probabilities(corpus)
    assert pytest.approx(sum(probabilities.values())) == 1.0


def test_nearer_seed_gets_higher_probability() -> None:
    corpus = make_corpus(make_seed("near", 1), make_seed("far", 50))
    probabilities = GuidedScheduler().selection_probabilities(corpus)
    assert probabilities["near"] > probabilities["far"]


def test_reached_seed_has_max_priority() -> None:
    corpus = make_corpus(make_seed("done", 0, reached=True), make_seed("near", 1))
    probabilities = GuidedScheduler().selection_probabilities(corpus)
    assert probabilities["done"] > probabilities["near"]


def test_unknown_distance_gets_zero_weight_but_stays_selectable() -> None:
    """距离未知的种子定向权重为 0,但 epsilon 仍能抽到它——不能饿死。"""
    scheduler = GuidedScheduler(epsilon=1.0, random_seed=0)
    corpus = make_corpus(make_seed("known", 1), make_seed("unknown", None))
    probabilities = scheduler.selection_probabilities(corpus)
    assert probabilities["unknown"] == 0.0
    picked = {scheduler.select(corpus).seed_id for _ in range(200)}
    assert "unknown" in picked


def test_all_unknown_distances_fall_back_to_uniform() -> None:
    corpus = make_corpus(make_seed("a", None), make_seed("b", None))
    probabilities = GuidedScheduler().selection_probabilities(corpus)
    assert probabilities == {"a": 0.5, "b": 0.5}


def test_selection_is_deterministic_for_seed() -> None:
    def run() -> list[str]:
        scheduler = GuidedScheduler(random_seed=7)
        corpus = make_corpus(make_seed("a", 1), make_seed("b", 2), make_seed("c", 3))
        return [scheduler.select(corpus).seed_id for _ in range(30)]

    assert run() == run()


def test_selection_increments_usage_counter() -> None:
    scheduler = GuidedScheduler(epsilon=0.0, random_seed=0)
    corpus = make_corpus(make_seed("only", 1))
    scheduler.select(corpus)
    scheduler.select(corpus)
    assert corpus.seeds[0].executions_selected == 2


def test_selected_seeds_get_penalised() -> None:
    """被选多次后权重下降:防止一个近种子把预算吃光。"""
    scheduler = GuidedScheduler()
    seed = make_seed("hot", 3)
    baseline = scheduler.selection_weight(seed)
    seed.executions_selected = 100
    assert scheduler.selection_weight(seed) < baseline


def test_energy_bounds_by_distance() -> None:
    scheduler = GuidedScheduler(min_energy=1, max_energy=32)
    near = scheduler.allocate_energy(make_seed("near", 0, reached=True))
    far = scheduler.allocate_energy(make_seed("far", 100))
    assert near == 32
    assert far == 1
    assert near >= far


def test_fixed_energy_policy_ignores_distance() -> None:
    scheduler = GuidedScheduler(energy_policy="fixed", fixed_energy=5)
    assert scheduler.allocate_energy(make_seed("near", 0, reached=True)) == 5
    assert scheduler.allocate_energy(make_seed("far", 999)) == 5


def test_uniform_scheduler_ignores_distance() -> None:
    scheduler = UniformScheduler(random_seed=0, energy=3)
    corpus = make_corpus(make_seed("near", 1), make_seed("far", 999))
    probabilities = scheduler.selection_probabilities(corpus)
    assert probabilities == {"near": 0.5, "far": 0.5}
    assert scheduler.allocate_energy(corpus.seeds[0]) == 3


def test_update_tracks_improvement() -> None:
    class _Result:
        distance = 2
        target_reached = False

    class _Better:
        distance = 1
        target_reached = False

    scheduler = GuidedScheduler()
    seed = make_seed("s", 5)
    weight_before = scheduler.selection_weight(seed)
    scheduler.update(seed, _Result())
    assert scheduler.selection_weight(seed) > weight_before
    scheduler.update(seed, _Better())
    assert scheduler.last_child_distance("s") == 1


def test_empty_corpus_raises() -> None:
    scheduler = GuidedScheduler()
    with pytest.raises(EmptyCorpusError):
        scheduler.select(GuidedCorpus())


def test_invalid_configuration_rejected() -> None:
    with pytest.raises(SchedulerError):
        GuidedScheduler(epsilon=1.5)
    with pytest.raises(SchedulerError):
        GuidedScheduler(min_energy=10, max_energy=2)
    with pytest.raises(SchedulerError):
        GuidedScheduler(selection_policy="bogus")
    with pytest.raises(SchedulerError):
        GuidedScheduler(energy_policy="bogus")
