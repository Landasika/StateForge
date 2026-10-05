"""Phase-5B:StateAnnotation IR——Repository Agent 的语义标注输出。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AnnotationSource(str, Enum):
    STATIC_OBSERVED = "STATIC_OBSERVED"   # Phase-5A 已观察到
    AGENT_INFERRED = "AGENT_INFERRED"      # Agent 从 repository 推断


class AnnotationStatus(str, Enum):
    SUCCESS = "SUCCESS"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    TIMEOUT = "TIMEOUT"
    NO_EVIDENCE = "NO_EVIDENCE"


@dataclass(frozen=True)
class RepositoryEvidence:
    file: str
    line_start: int
    line_end: int
    claim: str

    def to_dict(self) -> dict:
        return {"file": self.file, "line_start": self.line_start,
                "line_end": self.line_end, "claim": self.claim}


@dataclass
class AbstractStateValue:
    value: str
    source: AnnotationSource
    evidence: list[RepositoryEvidence] = field(default_factory=list)


@dataclass
class StateWriterAnnotation:
    function: str
    written_value: str | None
    evidence: list[RepositoryEvidence] = field(default_factory=list)


@dataclass
class StateReaderAnnotation:
    function: str
    evidence: list[RepositoryEvidence] = field(default_factory=list)


@dataclass
class StateGuardAnnotation:
    function: str
    predicate: str
    evidence: list[RepositoryEvidence] = field(default_factory=list)


@dataclass
class StateActionAnnotation:
    name: str
    evidence: list[RepositoryEvidence] = field(default_factory=list)


@dataclass
class StateTransitionCandidate:
    action: str
    from_value: str | None
    to_value: str | None
    evidence: list[RepositoryEvidence] = field(default_factory=list)
    confidence: float = 0.0


@dataclass
class StateAnnotation:
    candidate_id: str
    object_ref: str

    semantic_name: str | None = None
    semantic_description: str | None = None

    abstract_values: list[AbstractStateValue] = field(default_factory=list)
    writers: list[StateWriterAnnotation] = field(default_factory=list)
    readers: list[StateReaderAnnotation] = field(default_factory=list)
    guards: list[StateGuardAnnotation] = field(default_factory=list)
    related_actions: list[StateActionAnnotation] = field(default_factory=list)

    candidate_transitions: list[StateTransitionCandidate] = field(
        default_factory=list)
    evidence: list[RepositoryEvidence] = field(default_factory=list)

    confidence: float = 0.0
    backend: str = ""
    model: str | None = None
    status: AnnotationStatus = AnnotationStatus.SUCCESS

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "object_ref": self.object_ref,
            "semantic_name": self.semantic_name,
            "semantic_description": self.semantic_description,
            "abstract_values": [
                {"value": v.value, "source": v.source.value,
                 "evidence": [e.to_dict() for e in v.evidence]}
                for v in self.abstract_values],
            "writers": [
                {"function": w.function,
                 "written_value": w.written_value,
                 "evidence": [e.to_dict() for e in w.evidence]}
                for w in self.writers],
            "readers": [
                {"function": r.function,
                 "evidence": [e.to_dict() for e in r.evidence]}
                for r in self.readers],
            "guards": [
                {"function": g.function, "predicate": g.predicate,
                 "evidence": [e.to_dict() for e in g.evidence]}
                for g in self.guards],
            "related_actions": [
                {"name": a.name,
                 "evidence": [e.to_dict() for e in a.evidence]}
                for a in self.related_actions],
            "candidate_transitions": [
                {"action": t.action, "from_value": t.from_value,
                 "to_value": t.to_value, "confidence": t.confidence,
                 "evidence": [e.to_dict() for e in t.evidence]}
                for t in self.candidate_transitions],
            "evidence": [e.to_dict() for e in self.evidence],
            "confidence": self.confidence,
            "backend": self.backend,
            "model": self.model,
            "status": self.status.value,
        }


@dataclass
class AnnotationValidationResult:
    valid: bool
    schema_valid: bool = True
    binding_valid: bool = True
    evidence_valid: bool = True
    invalid_evidence: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
