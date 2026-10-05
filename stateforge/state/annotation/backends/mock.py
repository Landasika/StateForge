"""Phase-5B:MockRepositoryAnnotator——确定性 Mock(测试/CI 用)。"""

from __future__ import annotations

from pathlib import Path

from stateforge.state.annotation.model import (
    AbstractStateValue, AnnotationSource, AnnotationStatus,
    RepositoryEvidence, StateActionAnnotation, StateAnnotation,
    StateGuardAnnotation, StateReaderAnnotation, StateTransitionCandidate,
    StateWriterAnnotation)
from stateforge.state.index import ObjectUsage


def mock_annotate(state_fact: ObjectUsage,
                  repository_root: Path) -> StateAnnotation:
    """Deterministic mock:直接把 Phase-5A 观察包装成 annotation。"""
    abstract_values = [
        AbstractStateValue(value=v, source=AnnotationSource.STATIC_OBSERVED)
        for v in state_fact.observed_written_values]

    writers = [
        StateWriterAnnotation(
            function=w.get("function", ""),
            written_value=str(w.get("value", "")),
            evidence=[RepositoryEvidence(
                file=w.get("file", ""),
                line_start=w.get("line", 0), line_end=w.get("line", 0),
                claim=f"writes {state_fact.object_ref}")])
        for w in state_fact.writes]

    readers = [
        StateReaderAnnotation(
            function=r.get("function", ""),
            evidence=[RepositoryEvidence(
                file=r.get("file", ""),
                line_start=r.get("line", 0), line_end=r.get("line", 0),
                claim=f"reads {state_fact.object_ref}")])
        for r in state_fact.reads]

    guards = [
        StateGuardAnnotation(
            function=g.get("function", ""),
            predicate=f"{g.get('operator', '')} {g.get('value', '')}".strip(),
            evidence=[RepositoryEvidence(
                file=g.get("file", ""),
                line_start=g.get("line", 0), line_end=g.get("line", 0),
                claim=f"guards {state_fact.object_ref}")])
        for g in state_fact.guard_predicates]

    actions = sorted(
        {w.get("function", "") for w in state_fact.writes}
        | {r.get("function", "") for r in state_fact.reads}
        | {g.get("function", "") for g in state_fact.guard_predicates}
        - {""})

    transitions = [
        StateTransitionCandidate(
            action=w.get("function", ""),
            from_value=None,
            to_value=str(w.get("value", "")),
            confidence=0.5)
        for w in state_fact.writes if w.get("value")]

    return StateAnnotation(
        candidate_id=state_fact.object_ref,
        object_ref=state_fact.object_ref,
        semantic_name=state_fact.object_ref,
        semantic_description=None,
        abstract_values=abstract_values,
        writers=writers, readers=readers, guards=guards,
        related_actions=[StateActionAnnotation(name=a) for a in actions],
        candidate_transitions=transitions,
        evidence=(writers[0].evidence if writers else []),
        confidence=0.5, backend="mock", model=None,
        status=AnnotationStatus.SUCCESS)
