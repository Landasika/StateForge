"""Phase-5B:StateAnnotator 接口 + LLM backend + Mock。"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Protocol, runtime_checkable

from stateforge.state.annotation.model import (
    AbstractStateValue, AnnotationSource, AnnotationStatus,
    RepositoryEvidence, StateActionAnnotation, StateAnnotation,
    StateGuardAnnotation, StateReaderAnnotation, StateTransitionCandidate,
    StateWriterAnnotation)
from stateforge.state.index import StateFactIndex, ObjectUsage


@runtime_checkable
class StateAnnotator(Protocol):
    def annotate(self, state_fact: ObjectUsage,
                 repository_root: Path) -> StateAnnotation: ...


def _build_prompt(state_fact: ObjectUsage, repo_root: Path,
                  file_snippets: str) -> str:
    return f"""Investigate the persistent state fact below using repository evidence.

## Candidate
object_ref: {state_fact.object_ref}
domain: {state_fact.domain.value}

## Static Observations (Phase-5A)
reads: {[r.get("function") for r in state_fact.reads]}
writes: {[w.get("function") for w in state_fact.writes]}
deletes: {[d.get("function") for d in state_fact.deletes]}
guards: {[g.get("operator") for g in state_fact.guard_predicates]}
observed_written_values: {state_fact.observed_written_values}

## Repository Root
{repo_root}

## Available Source Context
{file_snippets}

## Task
Investigate this state fact. Determine:
1. A semantic name (e.g., "order lifecycle status")
2. Abstract values it can take
3. All writers (functions that modify it)
4. All readers (functions that read it)
5. All guards (functions that check it in a condition)
6. Related actions/routes
7. Candidate state transitions

Do NOT decide whether the application contains a vulnerability.
Do NOT modify the repository.

## Output (JSON only)
{{
  "semantic_name": "...",
  "semantic_description": "...",
  "abstract_values": [{{"value": "...", "source": "STATIC_OBSERVED|AGENT_INFERRED"}}],
  "writers": [{{"function": "...", "written_value": "...", "file": "...", "line_start": 0, "line_end": 0, "claim": "..."}}],
  "readers": [{{"function": "...", "file": "...", "line_start": 0, "line_end": 0, "claim": "..."}}],
  "guards": [{{"function": "...", "predicate": "...", "file": "...", "line_start": 0, "line_end": 0, "claim": "..."}}],
  "related_actions": [{{"name": "...", "file": "...", "line_start": 0, "line_end": 0, "claim": "..."}}],
  "candidate_transitions": [{{"action": "...", "from_value": "...", "to_value": "...", "confidence": 0.5, "file": "...", "line_start": 0, "line_end": 0, "claim": "..."}}]
}}"""


def _collect_snippets(repo_root: Path, max_files: int = 5,
                      max_lines: int = 80) -> str:
    """收集仓库中前 N 个 .py 文件的关键片段。"""
    snippets = []
    for py in sorted(repo_root.rglob("*.py"))[:max_files]:
        if "__pycache__" in str(py) or py.name.startswith("."):
            continue
        try:
            lines = py.read_text(encoding="utf-8").splitlines()
            if len(lines) > max_lines:
                lines = lines[:max_lines] + ["... (truncated)"]
            rel = py.relative_to(repo_root)
            snippets.append(f"### {rel}\n```python\n" + "\n".join(lines) + "\n```")
        except (OSError, UnicodeDecodeError):
            continue
    return "\n\n".join(snippets) if snippets else "(no source available)"


def _parse_annotation(raw: str, candidate_id: str, obj_ref: str,
                      backend: str, model: str | None) -> StateAnnotation:
    """解析 LLM 输出 → StateAnnotation。"""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.M)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return StateAnnotation(
            candidate_id=candidate_id, object_ref=obj_ref,
            status=AnnotationStatus.INVALID_OUTPUT, backend=backend,
            model=model)

    def _evidences(items):
        evs = []
        for item in (items or []):
            evs.append(RepositoryEvidence(
                file=item.get("file", ""),
                line_start=item.get("line_start", 0),
                line_end=item.get("line_end", 0),
                claim=item.get("claim", "")))
        return evs

    return StateAnnotation(
        candidate_id=candidate_id, object_ref=obj_ref,
        semantic_name=obj.get("semantic_name"),
        semantic_description=obj.get("semantic_description"),
        abstract_values=[
            AbstractStateValue(
                value=v.get("value", ""),
                source=AnnotationSource(
                    v.get("source", "AGENT_INFERRED")),
                evidence=_evidences(v.get("evidence_list", [])))
            for v in obj.get("abstract_values", [])],
        writers=[
            StateWriterAnnotation(
                function=w.get("function", ""),
                written_value=w.get("written_value"),
                evidence=_evidences([w]))
            for w in obj.get("writers", [])],
        readers=[
            StateReaderAnnotation(
                function=r.get("function", ""),
                evidence=_evidences([r]))
            for r in obj.get("readers", [])],
        guards=[
            StateGuardAnnotation(
                function=g.get("function", ""),
                predicate=g.get("predicate", ""),
                evidence=_evidences([g]))
            for g in obj.get("guards", [])],
        related_actions=[
            StateActionAnnotation(
                name=a.get("name", ""),
                evidence=_evidences([a]))
            for a in obj.get("related_actions", [])],
        candidate_transitions=[
            StateTransitionCandidate(
                action=t.get("action", ""),
                from_value=t.get("from_value"),
                to_value=t.get("to_value"),
                confidence=t.get("confidence", 0.0),
                evidence=_evidences([t]))
            for t in obj.get("candidate_transitions", [])],
        confidence=obj.get("confidence", 0.0),
        backend=backend, model=model,
        status=AnnotationStatus.SUCCESS)


class LLMRepositoryAnnotator:
    """使用现有 Provider(GLM proxy)的 repository annotator。"""

    def __init__(self, provider, max_snippet_files: int = 5):
        self.provider = provider
        self.max_files = max_snippet_files
        self.calls = 0
        self.total_time = 0.0

    def annotate(self, state_fact: ObjectUsage,
                 repository_root: Path) -> StateAnnotation:
        snippets = _collect_snippets(repository_root, self.max_files)
        prompt = _build_prompt(state_fact, repository_root, snippets)
        system = ("You are a repository semantic investigator. "
                  "Respond ONLY with the required JSON. "
                  "Do NOT identify vulnerabilities.")

        t0 = time.time()
        try:
            raw = self.provider.complete(system, prompt)
        except Exception:                          # noqa: BLE001
            return StateAnnotation(
                candidate_id="", object_ref=state_fact.object_ref,
                status=AnnotationStatus.PROVIDER_ERROR,
                backend="llm", model=getattr(self.provider, "model", None))
        self.total_time += time.time() - t0
        self.calls += 1

        return _parse_annotation(
            raw, state_fact.object_ref, state_fact.object_ref,
            "llm", getattr(self.provider, "model", None))
