"""provider.py —— SF6-T1 双供应商适配(ADR-0008:httpx 直调,不引 SDK)。

环境配置(优先级从高到低):
  SF_LLM_PROVIDER ∈ {anthropic, openai-compatible}   显式指定
  ANTHROPIC_API_KEY(+可选 ANTHROPIC_BASE_URL)         → anthropic(x-api-key)
  ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BASE_URL           → anthropic(Bearer;本机代理形态)
  OPENAI_API_KEY + SF_LLM_BASE_URL                    → openai-compatible(GLM 等)

无任何配置 → provider=None → 调用方走规则标注,链路永不阻塞。
超时 30s、重试 1 次、SF_LLM_MAX_CALLS 预算封顶(默认 64)。
"""

from __future__ import annotations

import os
from typing import Any

import httpx


class Provider:
    """极薄的消息发送抽象:prompt(str) → 文本(str)。"""

    def __init__(self, kind: str, base_url: str, headers: dict[str, str],
                 model: str, max_calls: int = 64):
        self.kind, self.base_url, self.headers = kind, base_url, headers
        self.model, self.max_calls = model, max_calls
        self.calls = 0

    # ---------------------------------------------------------------- 工厂

    @classmethod
    def from_env(cls) -> "Provider | None":
        explicit = os.getenv("SF_LLM_PROVIDER")
        anthropic_key = os.getenv("ANTHROPIC_API_KEY")
        anthropic_token = os.getenv("ANTHROPIC_AUTH_TOKEN")
        anthropic_base = os.getenv("ANTHROPIC_BASE_URL")
        openai_key = os.getenv("OPENAI_API_KEY")
        openai_base = os.getenv("SF_LLM_BASE_URL")
        model = os.getenv("SF_LLM_MODEL", "claude-sonnet-5")
        max_calls = int(os.getenv("SF_LLM_MAX_CALLS", "64"))

        if explicit == "anthropic" or (not explicit and (anthropic_key or anthropic_token)):
            if anthropic_key:
                headers = {"x-api-key": anthropic_key, "anthropic-version": "2023-06-01"}
            else:
                headers = {"Authorization": f"Bearer {anthropic_token}",
                           "anthropic-version": "2023-06-01"}
            base = (anthropic_base or "https://api.anthropic.com").rstrip("/")
            return cls("anthropic", base, headers, model, max_calls)
        if explicit == "openai-compatible" or (not explicit and openai_key and openai_base):
            return cls(
                "openai-compatible", openai_base.rstrip("/"),
                {"Authorization": f"Bearer {openai_key}"},
                os.getenv("SF_LLM_MODEL", "glm-4.6"), max_calls,
            )
        return None

    # ---------------------------------------------------------------- 调用

    def complete(self, system: str, user: str, temperature: float = 0.0) -> str:
        if self.calls >= self.max_calls:
            raise RuntimeError(f"SF_LLM_MAX_CALLS={self.max_calls} 预算耗尽")
        self.calls += 1
        if self.kind == "anthropic":
            payload: dict[str, Any] = {
                "model": self.model, "max_tokens": 4096,
                "temperature": temperature,
                # GLM anthropic 兼容层默认开 thinking(长思考会拖垮 30s 超时),显式关掉
                "thinking": {"type": "disabled"},
                "system": system, "messages": [{"role": "user", "content": user}],
            }
            # 兼容 GLM anthropic 代理:content 可能含 thinking 块,拼接全部 text 块
            url, out = (
                f"{self.base_url}/v1/messages",
                lambda r: "".join(b.get("text", "") for b in r.get("content", [])
                                  if isinstance(b, dict)),
            )
        else:
            payload = {
                "model": self.model, "temperature": temperature,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
            url, out = f"{self.base_url}/chat/completions", lambda r: r["choices"][0]["message"]["content"]

        last_exc: Exception | None = None
        # 自签名代理(如本机 GLM 代理)可跳过验证:SF_LLM_SSL_VERIFY=false
        _verify = os.getenv("SF_LLM_SSL_VERIFY", "true").lower() != "false"
        for _ in range(2):  # 1 次重试
            try:
                resp = httpx.post(url, json=payload, headers=self.headers,
                                  timeout=120.0, verify=_verify)
                resp.raise_for_status()
                return out(resp.json())
            except Exception as exc:  # noqa: BLE001 —— 重试一次后抛给调用方降级
                last_exc = exc
        raise RuntimeError(f"provider 调用失败({self.kind}): {last_exc}")
