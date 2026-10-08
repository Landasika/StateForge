"""Phase-4.2-D:MutationProposal IR(五状态,LLM 只产不做 oracle)。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ProposalStatus(str, Enum):
    PROPOSED = "PROPOSED"                     # 有候选修改,走校验→执行
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"  # 证据不够
    STATE_REQUIRED = "STATE_REQUIRED"         # 需持久状态(4.3 领地)
    UNSUPPORTED = "UNSUPPORTED"               # LLM 判定无法推理
    INVALID_OUTPUT = "INVALID_OUTPUT"         # 输出格式不合法


@dataclass
class MutationProposal:
    frontier_id: str
    source_kind: str                 # QUERY / FORM / JSON / HEADER / COOKIE
    source_key: str
    operation: str                   # REPLACE(第一版仅此一种)
    proposed_value: object
    rationale: str = ""
    confidence: float = 0.0
    status: ProposalStatus = ProposalStatus.PROPOSED
    raw_output: str = ""             # LLM 原始输出(审计)
