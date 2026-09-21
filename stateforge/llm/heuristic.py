"""llm/heuristic.py —— 无 LLM 时的降级(全 irrelevant,零白名单)。

**2026-09-21 白名单移除**:原关键词规则表(_RULES)已删。
语义判断的正式通道 = LLM `label()`;本函数只在无 LLM 且无缓存时
作为"诚实的不确定"降级——全部标 irrelevant,不猜。
"""

from __future__ import annotations

from stateforge.core.model import StateCandidate, StateVar


def label_rule_based(
    candidates: list[StateCandidate], domains: dict[str, list[str]]
) -> list[StateVar]:
    """无 LLM 降级:全部标 irrelevant(诚实的不确定,不猜)。

    语义判断的正式通道 = LLM label();本函数只在 provider=None 且缓存未命中时
    作为兜底——系统仍可运行,但无安全语义(SSTG 为空,规划器无链可排)。
    """
    out: dict[str, StateVar] = {}
    for cand in candidates:
        entity, _, attr = cand.var.partition(".")
        domain = domains.get(cand.var)
        if cand.var not in out:
            out[cand.var] = StateVar(
                entity=entity,
                attr=attr,
                kind="session" if entity == "Session" else "db_column",
                domain=domain,
                label="irrelevant",
                rationale="[no-llm] 无 LLM 且缓存未命中,不猜语义",
                anchors=[(cand.file, cand.line)] if cand.line else [],
            )
        else:
            sv = out[cand.var]
            if domain and (not sv.domain or len(domain) > len(sv.domain)):
                sv.domain = domain
            if (cand.file, cand.line) not in sv.anchors and cand.line:
                sv.anchors.append((cand.file, cand.line))
    return list(out.values())
