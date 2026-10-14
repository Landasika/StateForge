"""Phase-5A:StateFactIndex——按 object_ref 聚合 StateFact 候选。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from stateforge.state.model import StateFactCandidate, StateDomain


@dataclass
class ObjectUsage:
    """单个 object_ref 的聚合使用档案。"""
    object_ref: str
    domain: StateDomain
    reads: list[dict] = field(default_factory=list)
    writes: list[dict] = field(default_factory=list)
    deletes: list[dict] = field(default_factory=list)
    guard_predicates: list[dict] = field(default_factory=list)
    observed_written_values: list[Any] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "object_ref": self.object_ref,
            "domain": self.domain.value,
            "reads": self.reads,
            "writes": self.writes,
            "deletes": self.deletes,
            "guard_predicates": self.guard_predicates,
            "observed_written_values": self.observed_written_values,
        }


class StateFactIndex:
    """聚合索引:object_ref → 读写/守卫/写入值。"""

    def __init__(self):
        self.objects: dict[str, ObjectUsage] = {}

    def add(self, fact: StateFactCandidate) -> None:
        if fact.object_ref not in self.objects:
            self.objects[fact.object_ref] = ObjectUsage(
                fact.object_ref, fact.domain)
        obj = self.objects[fact.object_ref]

        entry = {
            "function": fact.function_name,
            "file": fact.source_file,
            "line": fact.lineno,
        }
        if fact.operation.value == "READ":
            obj.reads.append(entry)
        elif fact.operation.value == "WRITE":
            entry["value"] = fact.written_value
            obj.writes.append(entry)
            if fact.written_value is not None \
                    and fact.written_value != "UNKNOWN":
                if fact.written_value not in obj.observed_written_values:
                    obj.observed_written_values.append(fact.written_value)
        elif fact.operation.value == "DELETE":
            obj.deletes.append(entry)

        if fact.relevance.value == "CONTROL" and fact.structured_predicate:
            obj.guard_predicates.append({
                **fact.structured_predicate.to_dict(),
                "function": fact.function_name,
                "file": fact.source_file,
                "line": fact.lineno,
            })

    def writers_of(self, object_ref: str) -> list[dict]:
        """读点 → 可能影响它的写点(Phase 1 统一索引查询 API)。

        蕴含规则(行写覆盖字段):`db.orders.owner` 的读受 `db.orders.*`
        整行写影响;同表任意粒度互不影响字段级精确性以外的判断。
        事务点(db.session.commit/flush)不按 object_ref 匹配,不在此列。
        返回去重后的 [{object_ref, op, function, file, line, value}]。
        """
        out: dict[tuple, dict] = {}
        parent = (object_ref.rsplit(".", 1)[0]
                  if object_ref.count(".") >= 2 else None)
        for ref, obj in self.objects.items():
            if ref == "db.session":
                continue
            if ref not in ((object_ref, parent)
                           if parent else (object_ref,)):
                continue
            for kind, entries in (("WRITE", obj.writes),
                                  ("DELETE", obj.deletes)):
                for entry in entries:
                    key = (entry["file"], entry["line"], ref, kind)
                    out[key] = {
                        "object_ref": ref, "op": kind,
                        "function": entry["function"],
                        "file": entry["file"], "line": entry["line"],
                        "value": entry.get("value"),
                    }
        return [out[k] for k in sorted(out,
                                       key=lambda k: (k[0], k[1]))]

    def get_usage_profile(self, object_ref: str) -> str | None:
        """人读使用档案(给 HVG 用)。"""
        obj = self.objects.get(object_ref)
        if obj is None:
            return None
        lines = [object_ref]
        for w in obj.writes:
            v = w.get("value", "?")
            lines.append(f"  {w['function']}(): WRITE {v}")
        for r in obj.reads:
            lines.append(f"  {r['function']}(): READ")
        for d in obj.deletes:
            lines.append(f"  {d['function']}(): DELETE")
        for g in obj.guard_predicates:
            lines.append(f"  {g['function']}(): GUARD "
                        f"{g['operator']} {g.get('value', '')}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {ref: obj.to_dict()
                for ref, obj in sorted(self.objects.items())}

    def candidates_as_list(self) -> list[dict]:
        return [obj.to_dict() for _, obj in sorted(self.objects.items())]


def build_index(facts: list[StateFactCandidate]) -> StateFactIndex:
    idx = StateFactIndex()
    for f in facts:
        idx.add(f)
    return idx
