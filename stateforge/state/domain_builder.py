"""Phase-4.3 Domain Builder——从谓词自动推导抽象域。"""

from __future__ import annotations

from stateforge.state.semantic_model import (
    StateVariable, StatePredicate, PredicateOp)


class VarDomain:
    """单变量的抽象域:一个变量 → 一组互斥的抽象值。"""

    def __init__(self, variable: StateVariable):
        self.variable = variable
        self.rules: list[tuple[str, StatePredicate]] = []  # (name, pred)
        self._ensure_other()

    def add(self, name: str, pred: StatePredicate) -> None:
        self.rules.append((name, pred))
        self._ensure_other()

    def _ensure_other(self) -> None:
        if not any(n == "OTHER" for n, _ in self.rules):
            self.rules.append(("OTHER", None))

    def abstract(self, concrete) -> str:
        """具体值 → 抽象值名(第一个匹配的有谓词规则;无匹配→OTHER)。"""
        from stateforge.state.semantic_model import _eval
        for name, pred in self.rules:
            if pred is not None and _eval(pred, concrete):
                return name
        return "OTHER"

    def to_dict(self) -> dict:
        return {
            "variable": self.variable.canonical,
            "rules": [{"name": n, "pred": p.to_dict() if p else None}
                      for n, p in self.rules],
        }


def build_domains(predicates: list[StatePredicate]) -> dict[str, VarDomain]:
    """从全部谓词自动构建每个变量的抽象域。"""
    domains: dict[str, VarDomain] = {}

    for pred in predicates:
        var = pred.variable
        key = var.canonical
        if key not in domains:
            domains[key] = VarDomain(var)

        # 根据 op 生成抽象值名
        if pred.op == PredicateOp.EQ:
            name = f"EQ({pred.value})"
        elif pred.op == PredicateOp.IS_NONE:
            name = "NONE"
        elif pred.op == PredicateOp.IS_NOT_NONE:
            name = "NOT_NONE"
        elif pred.op in (PredicateOp.GE, PredicateOp.GT):
            name = f">={pred.value}" if pred.op == PredicateOp.GE else f">{pred.value}"
        else:
            name = f"{pred.op.value}({pred.value})"

        if not any(n == name for n, _ in domains[key].rules):
            domains[key].add(name, pred)

    return domains
