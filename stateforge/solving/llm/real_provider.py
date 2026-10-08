"""Phase-4.2-F:Real LLM Provider(env-var 配置,极薄——只序列化/调用/解析)。

不做 validation/mutation/coverage/retry;错误包装为 ProviderError,
由 coverage_loop 统一降级为 INVALID_OUTPUT。
"""

from __future__ import annotations

import json
import os
from pathlib import Path


class ProviderError(Exception):
    """API 超时/异常/格式错——统一异常,不 crash campaign。"""


class RealLLMProvider:
    """调本机 GLM 代理(OpenAI 兼容)或远程 API;env-var 控制。

    STATEFORGE_LLM_API_KEY   必填
    STATEFORGE_LLM_BASE_URL  默认 https://open.bigmodel.cn/api/paas/v4
    STATEFORGE_LLM_MODEL     默认 glm-4-flash
    STATEFORGE_LLM_TIMEOUT   默认 30 秒
    """

    def __init__(self):
        self.api_key = os.environ.get("STATEFORGE_LLM_API_KEY", "")
        self.base_url = os.environ.get(
            "STATEFORGE_LLM_BASE_URL",
            "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
        self.model = os.environ.get("STATEFORGE_LLM_MODEL", "glm-4-flash")
        self.timeout = int(os.environ.get("STATEFORGE_LLM_TIMEOUT", "30"))

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def complete(self, system: str, user: str) -> str:
        """唯一接口:system+user → raw response string。"""
        if not self.configured:
            raise ProviderError("STATEFORGE_LLM_API_KEY not set")
        import httpx
        try:
            resp = httpx.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                json={"model": self.model,
                      "messages": [
                          {"role": "system", "content": system},
                          {"role": "user", "content": user}],
                      "temperature": 0.1,
                      "max_tokens": 500},
                timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except httpx.TimeoutException:
            raise ProviderError(f"LLM timeout after {self.timeout}s")
        except httpx.HTTPStatusError as exc:
            raise ProviderError(f"LLM HTTP {exc.response.status_code}")
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            raise ProviderError(f"LLM response parse error: {exc}")
        except Exception as exc:               # noqa: BLE001
            raise ProviderError(f"LLM provider error: {exc}")


class MockLLMProvider:
    """测试用:预设响应序列,耗尽后返回 UNSUPPORTED。"""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        if self.calls < len(self.responses):
            resp = self.responses[self.calls]
        else:
            resp = json.dumps({"status": "UNSUPPORTED", "source_kind": "",
                               "source_key": "", "operation": "REPLACE",
                               "proposed_value": None,
                               "rationale": "exhausted", "confidence": 0})
        self.calls += 1
        return resp
