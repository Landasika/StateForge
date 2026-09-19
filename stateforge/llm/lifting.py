"""lifting.py —— SF6-T2 候选锚定标注协议(ADR-0009)。

  label(candidates, domains)  → [StateVar]   逐候选六类枚举标签+rationale
  relate(relevant_vars, eps)  → LiftResult   有界关系提升(角色层级/补迁移/跨实体不变式)

行为契约:
  - provider=None(无 key)→ 规则标注(llm/heuristic.py 的 label_rule_based)
  - LLM 输出强 JSON 校验,失败重试 1 次,再失败 → 该批降级规则标注(永不阻塞)
  - 缓存:sha256(候选事实+提示词版本),同输入二次零 token
  - 温度 0;模型与版本记录在 LiftResult.meta(实验可复现)
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from stateforge.core.model import Endpoint, StateCandidate, StateVar
from stateforge.llm import cache
from stateforge.llm.heuristic import label_rule_based
from stateforge.llm.provider import Provider

PROMPT_VERSION = "v1"
_PROMPTS = (Path(__file__).parent / "prompts" / f"{PROMPT_VERSION}.md").read_text(encoding="utf-8")
_LABEL_SYSTEM = _PROMPTS.split("## label(system)")[1].split("## relate(system)")[0].strip()
_RELATE_SYSTEM = _PROMPTS.split("## relate(system)")[1].strip()

VALID_LABELS = {"authn", "authz", "ownership", "money", "workflow", "single_use", "irrelevant"}


class ExtraTransition(BaseModel):
    endpoint: str
    var: str
    frm: str = Field(alias="from")
    to: str

    model_config = {"populate_by_name": True}


class CrossEntityInvariant(BaseModel):
    vars: list[str]
    rule: str


class LiftResult(BaseModel):
    source: str = "heuristic"            # heuristic | llm:<provider>:<model>
    role_hierarchy: dict[str, list[str]] = Field(default_factory=dict)
    extra_transitions: list[ExtraTransition] = Field(default_factory=list)
    cross_entity_invariants: list[CrossEntityInvariant] = Field(default_factory=list)
    label_log: list[dict] = Field(default_factory=list)   # 逐候选标签审计记录
    meta: dict = Field(default_factory=dict)             # 模型/温度/缓存命中


# ---------------------------------------------------------------- label


def label(candidates: list[StateCandidate], domains: dict[str, list[str]],
          provider: Provider | None = None) -> tuple[list[StateVar], LiftResult]:
    """返回 (StateVar 列表, LiftResult 审计)。provider=None 走规则。"""
    if provider is None:
        return label_rule_based(candidates, domains), LiftResult(source="heuristic")

    facts = [
        {"var": c.var, "ops": c.ops, "source": c.source,
         "domain": domains.get(c.var), "evidence": c.evidence[:160]}
        for c in candidates
    ]
    raw = _cached_complete(provider, "label", facts, _LABEL_SYSTEM)
    parsed = _parse_labels(raw, facts)
    if parsed is None:  # 一次结构化重试
        raw = _cached_complete(provider, "label", facts, _LABEL_SYSTEM + "\n(previous answer was not valid JSON — output ONLY the JSON array)", refresh=True)
        parsed = _parse_labels(raw, facts)
    if parsed is None:  # 降级
        return label_rule_based(candidates, domains), LiftResult(
            source="heuristic", meta={"fallback": "schema_violation"},
        )

    by_var = {p["var"]: p for p in parsed}
    out = label_rule_based(candidates, domains)  # 结构打底(anchors/domain 合并)
    for sv in out:
        hit = by_var.get(sv.var)
        if hit:
            sv.label = hit["label"]
            sv.rationale = f"[llm:{provider.model}] {hit['rationale']}"
    log = [{"var": p["var"], "label": p["label"], "confidence": p.get("confidence")}
           for p in parsed]
    return out, LiftResult(
        source=f"llm:{provider.kind}:{provider.model}", label_log=log,
        meta={"temperature": 0.0, "prompt": PROMPT_VERSION, "model": provider.model},
    )


def _parse_labels(raw: str, facts: list[dict]) -> list[dict] | None:
    try:
        arr = json.loads(_strip_fences(raw))
    except json.JSONDecodeError:
        return None
    if not isinstance(arr, list):
        return None
    want_vars = {f["var"] for f in facts}
    got = set()
    out = []
    for item in arr:
        if not isinstance(item, dict) or item.get("var") not in want_vars:
            continue
        if item.get("label") not in VALID_LABELS:
            continue
        if item["var"] in got:
            continue
        got.add(item["var"])
        out.append(item)
    return out if got == want_vars else None


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _cached_complete(provider: Provider, kind: str, facts, system: str,
                     refresh: bool = False) -> str:
    if not refresh:
        hit = cache.get({"kind": kind, "facts": facts}, PROMPT_VERSION)
        if hit is not None:
            return hit
    raw = provider.complete(system, json.dumps(facts, ensure_ascii=False))
    cache.put({"kind": kind, "facts": facts}, PROMPT_VERSION, raw)
    return raw


# ---------------------------------------------------------------- relate


def relate(relevant_vars: list[StateVar], endpoints: list[Endpoint],
           provider: Provider | None = None) -> LiftResult:
    """有界关系提升:只对 security-relevant 候选做(ADR-0009)。"""
    if provider is None or not relevant_vars:
        return LiftResult(source="heuristic")
    facts = {
        "vars": [
            {"var": sv.var, "label": sv.label, "domain": sv.domain}
            for sv in relevant_vars
        ],
        "endpoints": [
            {"route": f"{ep.methods[0]} {ep.route}",
             "pre": [[p.var, p.op, p.value] for p in ep.preconditions],
             "eff": [[e.var, e.value] for e in ep.effects]}
            for ep in endpoints
        ],
    }
    raw = _cached_complete(provider, "relate", facts, _RELATE_SYSTEM)
    try:
        data = json.loads(_strip_fences(raw))
        return LiftResult(
            source=f"llm:{provider.kind}:{provider.model}",
            role_hierarchy=data.get("role_hierarchy") or {},
            extra_transitions=[ExtraTransition.model_validate(t)
                               for t in data.get("extra_transitions") or []],
            cross_entity_invariants=[CrossEntityInvariant.model_validate(i)
                                     for i in data.get("cross_entity_invariants") or []],
            meta={"temperature": 0.0, "prompt": PROMPT_VERSION, "model": provider.model},
        )
    except (json.JSONDecodeError, ValidationError):
        return LiftResult(source="heuristic", meta={"fallback": "schema_violation"})
