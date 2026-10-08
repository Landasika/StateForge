"""Phase-4.2-D:LLM Semantic Frontier Solver(FrontierSemanticSlice → MutationProposal)。

LLM 只产 proposal,不做 oracle:crossing 裁决权仍在 Phase-1 mapper。
输入唯一上下文 = 七项 Slice(不给完整仓库)。
Prompt 固定(实验前冻结),不根据结果调整。
"""

from __future__ import annotations

import json
import re
import time

from stateforge.solving.proposal import MutationProposal, ProposalStatus

SYSTEM_PROMPT = """You are a program analysis assistant. Given a Frontier Semantic Slice of a Python web application, your ONLY task is to determine if a mutation to the CURRENT REQUEST's input field can make the target guard condition more likely to be satisfied.

Rules:
1. You may ONLY propose a mutation to the input source specified in the slice (kind and key).
2. You may NOT modify program logic, source code, or propose multi-request sequences.
3. If the guard depends on persistent state (session, database, file system), return STATE_REQUIRED.
4. If the slice does not contain enough information to infer a candidate, return INSUFFICIENT_EVIDENCE.
5. If the transformation is too complex for you to reason about, return UNSUPPORTED.
6. You may NOT claim that a frontier has been crossed — only actual execution can determine that.

Respond ONLY with a JSON object (no markdown, no explanation outside JSON):
{
  "status": "PROPOSED" | "INSUFFICIENT_EVIDENCE" | "STATE_REQUIRED" | "UNSUPPORTED",
  "source_kind": "<must match slice input_source.kind>",
  "source_key": "<must match slice input_source.key>",
  "operation": "REPLACE",
  "proposed_value": <the value to set>,
  "rationale": "<brief explanation>",
  "confidence": <0.0-1.0>
}"""

USER_TEMPLATE = """## Frontier Semantic Slice

```json
{slice_json}
```

Analyze this slice and return a MutationProposal JSON."""


def _parse_llm_output(raw: str, frontier_id: str) -> MutationProposal:
    """解析 LLM 原始输出;格式错误 → INVALID_OUTPUT。"""
    # 剥掉可能的 markdown 围栏
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.M)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return MutationProposal(
            frontier_id=frontier_id, source_kind="", source_key="",
            operation="", proposed_value=None,
            status=ProposalStatus.INVALID_OUTPUT, raw_output=raw)
    status_str = obj.get("status", "")
    try:
        status = ProposalStatus(status_str)
    except ValueError:
        return MutationProposal(
            frontier_id=frontier_id,
            source_kind=obj.get("source_kind", ""),
            source_key=obj.get("source_key", ""),
            operation=obj.get("operation", ""),
            proposed_value=obj.get("proposed_value"),
            rationale=obj.get("rationale", ""),
            confidence=obj.get("confidence", 0.0),
            status=ProposalStatus.INVALID_OUTPUT, raw_output=raw)
    return MutationProposal(
        frontier_id=frontier_id,
        source_kind=obj.get("source_kind", ""),
        source_key=obj.get("source_key", ""),
        operation=obj.get("operation", "REPLACE"),
        proposed_value=obj.get("proposed_value"),
        rationale=obj.get("rationale", ""),
        confidence=obj.get("confidence", 0.0),
        status=status, raw_output=raw)


class LLMSemanticSolver:
    """封装 LLM 调用:输入 slice,输出 MutationProposal。"""

    def __init__(self, provider):
        """provider: 有 .complete(system, user) -> str 的对象
        (stateforge/solving/llm/provider.py 的双适配,或测试 mock)。"""
        self.provider = provider

    def solve(self, slice_: dict) -> tuple[MutationProposal, float, dict]:
        """返回 (proposal, latency_seconds, token_usage_dict)。"""
        slice_json = json.dumps(slice_, ensure_ascii=False, indent=1)
        t0 = time.monotonic()
        raw = self.provider.complete(SYSTEM_PROMPT,
                                     USER_TEMPLATE.format(
                                         slice_json=slice_json))
        latency = time.monotonic() - t0
        frontier_id = (f"{slice_['target_frontier']['edge'][0]}"
                       f"->{slice_['target_frontier']['edge'][1]}")
        proposal = _parse_llm_output(raw, frontier_id)
        return proposal, latency, {"raw_length": len(raw)}