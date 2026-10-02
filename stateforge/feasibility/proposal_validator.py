"""Phase-4.2-D:ProposalValidator——LLM 与候选生成之间的 trust boundary。

六项校验:frontier 存在 / source kind 与 slice 一致 / source key 与
provenance 一致 / operation 允许 / value 可序列化 / 不重复 failed_mutations。
LLM 无权创造 slice 中不存在的输入源。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from stateforge.feasibility.proposal import MutationProposal, ProposalStatus

_ALLOWED_OPS = {"REPLACE"}
_KIND_TO_ATTR = {"QUERY": "args", "FORM": "form", "JSON": "json",
                 "HEADER": "headers", "COOKIE": "cookies"}


@dataclass
class ValidationResult:
    valid: bool
    rejection_reason: str = ""


def validate(proposal: MutationProposal, slice_: dict) -> ValidationResult:
    tf = slice_["target_frontier"]
    src = slice_["input_source"]

    if proposal.frontier_id != f"{tf['edge'][0]}->{tf['edge'][1]}":
        return ValidationResult(False, "FRONTIER_MISMATCH")

    if src["status"] == "ok":
        # provenance 成功 → 严格对齐(LLM 无权改来源)
        if proposal.source_kind != src["kind"]:
            return ValidationResult(False, "SOURCE_KIND_MISMATCH")
        if proposal.source_key != src["key"]:
            return ValidationResult(False, "SOURCE_KEY_MISMATCH")
    else:
        # provenance 失败(UNRESOLVED/SOURCE_UNKNOWN/AMBIGUOUS)→ 检查
        # LLM 提出的来源是否在 backward_dataflow 文本中出现过
        # (trust boundary:不得发明 slice 中不存在的输入源)
        # 通用 key 匹配:request.args["x"] / q.get("x") / json["x"] 等
        # 只要 key 在数据流文本中出现即接受(LLM 不能发明不存在的字段)
        key = re.escape(proposal.source_key)
        patterns = [
            rf"request\.[a-z]+\[.{key}.\]",       # request.args["x"]
            rf"\.get\(.{key}.\)",                   # q.get("x")
            rf"\[.{key}\]",                          # dict["x"]
        ]
        dataflow_text = " ".join(slice_.get("backward_dataflow", []))
        if not any(re.search(p, dataflow_text) for p in patterns):
            return ValidationResult(False,
                                    "SOURCE_NOT_IN_DATAFLOW_SLICE")

    if proposal.operation not in _ALLOWED_OPS:
        return ValidationResult(False, "OPERATION_NOT_ALLOWED")
    try:
        str(proposal.proposed_value)
    except Exception:                          # noqa: BLE001
        return ValidationResult(False, "VALUE_NOT_SERIALIZABLE")
    for fm in slice_.get("failed_mutations", []):
        if str(fm.get("input_id")) == str(proposal.proposed_value):
            return ValidationResult(False, "DUPLICATES_FAILED_MUTATION")
    return ValidationResult(True)
