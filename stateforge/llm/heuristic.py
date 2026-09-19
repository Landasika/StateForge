"""llm/heuristic.py —— SF1-T7 规则标注(对标实验的 heuristic 列;零 LLM)。

六类标签按关键词命中(优先级从上到下);domain 由字面量聚合传入。
这正是 SF6 三方对标中 LLM `label()` 要替换的实现——接口签名保持一致。
"""

from __future__ import annotations

from stateforge.analyzer.idioms import single_use_attrs
from stateforge.core.model import StateCandidate, StateLabel, StateVar

_RULES: list[tuple[StateLabel, tuple[str, ...]]] = [
    ("authn", ("authed_as", "token", "session", "jwt", "password", "credential")),
    ("authz", ("role", "admin", "permission", "privilege")),
    ("ownership", ("user_id", "owner")),
    ("money", ("total", "paid_amount", "amount", "balance", "price", "discount", "quota")),
    ("single_use", tuple(single_use_attrs())),
    ("workflow", ("status", "state", "phase", "stage")),
]


def _label_for(var: str) -> StateLabel:
    lowered = var.lower()
    for label, keys in _RULES:
        if any(k in lowered for k in keys):
            return label
    return "irrelevant"


def label_rule_based(
    candidates: list[StateCandidate], domains: dict[str, list[str]]
) -> list[StateVar]:
    """候选 → StateVar(带六类标签 + rationale);域来自字面量聚合。"""
    out: dict[str, StateVar] = {}
    for cand in candidates:
        entity, _, attr = cand.var.partition(".")
        label = _label_for(cand.var)
        domain = domains.get(cand.var)
        if cand.var not in out:
            out[cand.var] = StateVar(
                entity=entity,
                attr=attr,
                kind="session" if entity == "Session" else "db_column",
                domain=domain,
                label=label,
                rationale=_rationale(label, cand),
                anchors=[(cand.file, cand.line)] if cand.line else [],
            )
        else:
            sv = out[cand.var]
            if domain and (not sv.domain or len(domain) > len(sv.domain)):
                sv.domain = domain
            if (cand.file, cand.line) not in sv.anchors and cand.line:
                sv.anchors.append((cand.file, cand.line))
            if label != "irrelevant" and sv.label == "irrelevant":
                sv.label = label
    return list(out.values())


def _rationale(label: StateLabel, cand: StateCandidate) -> str:
    why = {
        "authn": "认证链变量(token/session)",
        "authz": "角色/权限判定变量",
        "ownership": "实体所有者绑定",
        "money": "资金/额度字段",
        "single_use": "单次性布尔(单向翻转)",
        "workflow": "业务状态机枚举属性",
        "irrelevant": "无六类关键词命中",
    }[label]
    return f"[rule] {why};证据:{cand.evidence[:80]}"
