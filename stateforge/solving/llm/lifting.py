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
from stateforge.solving.llm import cache
from stateforge.solving.llm.heuristic import label_rule_based
from stateforge.solving.llm.provider import Provider

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


class AuthChain(BaseModel):
    """LLM relate 提出的 auth 链:该端点是否产/需认证状态。"""
    endpoint: str                     # "POST /auth/login"
    direction: str                    # "produce" | "require" | "clear"
    rationale: str = ""


class CrossEntityInvariant(BaseModel):
    vars: list[str]
    rule: str


class LiftResult(BaseModel):
    source: str = "heuristic"            # heuristic | llm:<provider>:<model> | llm:cache
    role_hierarchy: dict[str, list[str]] = Field(default_factory=dict)
    extra_transitions: list[ExtraTransition] = Field(default_factory=list)
    cross_entity_invariants: list[CrossEntityInvariant] = Field(default_factory=list)
    auth_chains: list[AuthChain] = Field(default_factory=list)  # LLM auth 链(替代 auth_baseline)
    label_log: list[dict] = Field(default_factory=list)   # 逐候选标签审计记录
    meta: dict = Field(default_factory=dict)             # 模型/温度/缓存命中


# ---------------------------------------------------------------- label


def label(candidates: list[StateCandidate], domains: dict[str, list[str]],
          provider: Provider | None = None) -> tuple[list[StateVar], LiftResult]:
    """返回 (StateVar 列表, LiftResult 审计)。

    优先级:① 缓存命中(即使无 provider,历史 LLM 响应仍可用)
           ② provider 调用
           ③ 无 provider 且无缓存 → 全 irrelevant(诚实降级,不猜)
    """
    facts = [
        {"var": c.var, "ops": c.ops, "source": c.source,
         "domain": domains.get(c.var), "evidence": c.evidence[:160]}
        for c in candidates
    ]

    # ① 缓存优先(测试/离线复现:跑一次真 LLM 后,后续零 token)
    cached_raw = cache.get({"kind": "label", "facts": facts}, PROMPT_VERSION)
    if cached_raw is not None:
        parsed = _parse_labels(cached_raw, facts)
        if parsed is not None:
            return _apply_labels(parsed, candidates, domains,
                                 LiftResult(source="llm:cache",
                                            meta={"from": "cache"}))

    # ② 无缓存且无 provider → 诚实降级
    if provider is None:
        return label_rule_based(candidates, domains), LiftResult(source="no-llm")

    # ③ 真调用
    raw = _cached_complete(provider, "label", facts, _LABEL_SYSTEM)
    parsed = _parse_labels(raw, facts)
    if parsed is None:  # 一次结构化重试
        raw = _cached_complete(provider, "label", facts,
                               _LABEL_SYSTEM + "\n(previous answer was not valid JSON — output ONLY the JSON array)",
                               refresh=True)
        parsed = _parse_labels(raw, facts)
    if parsed is None:  # 降级
        return label_rule_based(candidates, domains), LiftResult(
            source="no-llm", meta={"fallback": "schema_violation"},
        )
    return _apply_labels(parsed, candidates, domains,
                         LiftResult(source=f"llm:{provider.kind}:{provider.model}",
                                    meta={"temperature": 0.0, "prompt": PROMPT_VERSION,
                                          "model": provider.model}))


def _apply_labels(parsed: list[dict], candidates: list[StateCandidate],
                  domains: dict[str, list[str]], lift: LiftResult
                  ) -> tuple[list[StateVar], LiftResult]:
    """LLM 解析结果应用到 StateVar(结构打底+标签覆盖)。"""
    by_var = {p["var"]: p for p in parsed}
    out = label_rule_based(candidates, domains)  # 结构打底(anchors/domain 合并)
    for sv in out:
        hit = by_var.get(sv.var)
        if hit:
            sv.label = hit["label"]
            sv.rationale = f"[{lift.source}] {hit['rationale']}"
    lift.label_log = [{"var": p["var"], "label": p["label"],
                       "confidence": p.get("confidence")} for p in parsed]
    return out, lift


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
    """有界关系提升:只对 security-relevant 候选做(ADR-0009)。

    产出:角色层级/补迁移/跨实体不变式 + **auth 链**(产/需/清认证状态)。
    """
    if not relevant_vars:
        return LiftResult(source="no-llm")
    facts = {
        "vars": [
            {"var": sv.var, "label": sv.label, "domain": sv.domain}
            for sv in relevant_vars
        ],
        "endpoints": [
            {"route": f"{ep.methods[0]} {ep.route}",
             "pre": [[p.var, p.op, p.value] for p in ep.preconditions],
             "eff": [[e.var, e.value] for e in ep.effects],
             "observes": ep.observes[:6]}
            for ep in endpoints
        ],
    }

    # 缓存优先
    cached_raw = cache.get({"kind": "relate", "facts": facts}, PROMPT_VERSION)
    if cached_raw is not None:
        return _parse_relate(cached_raw, LiftResult(source="llm:cache"))

    if provider is None:
        return LiftResult(source="no-llm")

    raw = _cached_complete(provider, "relate", facts, _RELATE_SYSTEM)
    return _parse_relate(raw, LiftResult(
        source=f"llm:{provider.kind}:{provider.model}",
        meta={"temperature": 0.0, "prompt": PROMPT_VERSION, "model": provider.model}))


def _parse_relate(raw: str, lift: LiftResult) -> LiftResult:
    try:
        data = json.loads(_strip_fences(raw))
        lift.role_hierarchy = data.get("role_hierarchy") or {}
        lift.extra_transitions = [ExtraTransition.model_validate(t)
                                  for t in data.get("extra_transitions") or []]
        lift.cross_entity_invariants = [CrossEntityInvariant.model_validate(i)
                                        for i in data.get("cross_entity_invariants") or []]
        lift.auth_chains = [AuthChain.model_validate(a)
                            for a in data.get("auth_chains") or []]
        return lift
    except (json.JSONDecodeError, ValidationError):
        return LiftResult(source="no-llm", meta={"fallback": "schema_violation"})


def apply_lift_result(model, lift: LiftResult) -> int:
    """把 LLM 语义结果应用到 AppModel。

    两路:
    ① auth_chains(relate 的显式输出)→ Session.authed_as 产/需/清
    ② 结构推导:端点调了 LLM 标为 authn 的函数 → 该端点需认证
       (不是白名单:authn 判断来自 LLM,这里只做"调了 authn 函数→需 auth"的
        结构推理)
    """
    from stateforge.core.model import Effect, Precondition, StateCandidate

    added = 0
    sec_entities = {sv.var.split(".")[0] for sv in model.state_vars
                    if sv.security_relevant}

    # ① 显式 auth_chains
    #    produce 只加给纯认证端点(除 auth 外无其他安全实体效应)——
    #    避免 register 被误标为 session 生产者(LLM 倾向把"使后续登录可行"
    #    也归为 produce,但我们要的是"直接建立会话/返回令牌")
    for chain in lift.auth_chains:
        route = chain.endpoint
        for ep in model.endpoints:
            if f"{ep.methods[0]} {ep.route}" == route or ep.route == route:
                if chain.direction == "produce":
                    has_auth = any(e.var == "Session.authed_as" for e in ep.effects)
                    other_sec_effs = [
                        e for e in ep.effects
                        if e.var != "Session.authed_as"
                        and e.var.split(".")[0] in sec_entities
                    ]
                    if not has_auth and not other_sec_effs:
                        ep.effects.append(Effect(
                            var="Session.authed_as", op="set", key="username",
                            file=ep.file, line=ep.line))
                        added += 1
                elif chain.direction == "require":
                    if not any(p.var == "Session.authed_as" for p in ep.preconditions):
                        ep.preconditions.append(Precondition(
                            var="Session.authed_as", op="truthy",
                            file=ep.file, line=ep.line))
                        added += 1
                elif chain.direction == "clear":
                    if not any(e.var == "Session.authed_as" and e.op == "clear"
                               for e in ep.effects):
                        ep.effects.append(Effect(
                            var="Session.authed_as", op="clear",
                            file=ep.file, line=ep.line))
                        added += 1
                break

    # ② 结构推导:写/读安全实体 → 需认证(结构默认,非关键词白名单)
    if sec_entities:
        auth_ep_routes = {"/auth/register", "/auth/login", "/"}
        for ep in model.endpoints:
            has_pre = any(p.var == "Session.authed_as" for p in ep.preconditions)
            is_public = ep.route in auth_ep_routes or ep.route.startswith("/auth/")
            touches_sec = any(
                e.var.split(".")[0] in sec_entities for e in ep.effects
            ) or any(
                p.var.split(".")[0] in sec_entities for p in ep.preconditions
            )
            if touches_sec and not has_pre and not is_public:
                ep.preconditions.append(Precondition(
                    var="Session.authed_as", op="truthy",
                    file=ep.file, line=ep.line))
                added += 1

    # Session.authed_as 候选+StateVar 若不存在,补
    if added:
        from stateforge.core.model import StateVar
        if not any(c.var == "Session.authed_as" for c in model.candidates):
            model.candidates.append(StateCandidate(
                var="Session.authed_as", entity="Session", attr="authed_as",
                ops=["write", "read"],
                evidence="LLM auth_chains + structural inference",
                source="session_access", file="", line=0))
        if not any(sv.var == "Session.authed_as" for sv in model.state_vars):
            model.state_vars.append(StateVar(
                entity="Session", attr="authed_as", kind="session",
                label="authn", security_relevant=True,
                rationale=f"[{lift.source}] LLM auth chain + structural inference"))
    return added
