"""Phase-5B:AnnotationValidator——四层验证(schema/binding/existence/lexical)。"""

from __future__ import annotations

from pathlib import Path

from stateforge.state.annotation.model import (
    AnnotationValidationResult, StateAnnotation)


def validate_annotation(annotation: StateAnnotation,
                        repository_root: Path) -> AnnotationValidationResult:
    """四层验证:schema → binding → evidence existence → lexical support。"""

    # V1: Schema
    schema_ok = (
        bool(annotation.object_ref)
        and annotation.confidence is not None
        and 0.0 <= annotation.confidence <= 1.0
        and annotation.status is not None)
    if not schema_ok:
        return AnnotationValidationResult(
            valid=False, schema_valid=False,
            warnings=["schema validation failed"])

    # V2: Candidate Binding
    if annotation.candidate_id != annotation.object_ref:
        return AnnotationValidationResult(
            valid=False, schema_valid=True, binding_valid=False,
            warnings=["candidate_id != object_ref"])

    # V3: Evidence Existence
    invalid_evidence = []
    all_evidence = list(annotation.evidence)
    for w in annotation.writers:
        all_evidence.extend(w.evidence)
    for r in annotation.readers:
        all_evidence.extend(r.evidence)
    for g in annotation.guards:
        all_evidence.extend(g.evidence)
    for a in annotation.related_actions:
        all_evidence.extend(a.evidence)
    for t in annotation.candidate_transitions:
        all_evidence.extend(t.evidence)

    evidence_ok = True
    for ev in all_evidence:
        if not ev.file:
            invalid_evidence.append({"reason": "empty_file", "claim": ev.claim})
            evidence_ok = False
            continue
        file_path = repository_root / ev.file
        if not file_path.exists():
            invalid_evidence.append(
                {"reason": "file_not_found", "file": ev.file,
                 "claim": ev.claim})
            evidence_ok = False
            continue
        if ev.line_start < 1 or ev.line_end < ev.line_start:
            invalid_evidence.append(
                {"reason": "invalid_line_range",
                 "file": ev.file, "line": f"{ev.line_start}-{ev.line_end}",
                 "claim": ev.claim})
            evidence_ok = False
            continue
        try:
            line_count = len(file_path.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeDecodeError):
            continue
        if ev.line_end > line_count:
            invalid_evidence.append(
                {"reason": "line_end_exceeds",
                 "file": ev.file, "line": ev.line_end,
                 "claim": ev.claim})
            evidence_ok = False

    # V4: Lexical Support(保守)
    warnings = []
    for ev in all_evidence:
        if not ev.file or ev.claim == "":
            continue
        file_path = repository_root / ev.file
        if not file_path.exists():
            continue
        try:
            lines = file_path.read_text(encoding="utf-8").splitlines()
            segment = "\n".join(lines[max(0, ev.line_start - 1):ev.line_end])
        except (OSError, UnicodeDecodeError):
            continue
        # 检查 claim 中的 object_ref 关键词是否出现在代码段
        ref_parts = annotation.object_ref.split(".")
        key = ref_parts[-1] if ref_parts else annotation.object_ref
        if key and key not in segment and key not in ev.claim:
            warnings.append(
                f"lexical: '{key}' not in {ev.file}:{ev.line_start}")

    return AnnotationValidationResult(
        valid=schema_ok and evidence_ok,
        schema_valid=schema_ok,
        binding_valid=True,
        evidence_valid=evidence_ok,
        invalid_evidence=invalid_evidence,
        warnings=warnings)
