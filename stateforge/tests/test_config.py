"""config 单测:llm_settings(STATEFORGE_LLM_*)与 legacy_provider_settings
(ANTHROPIC_*/SF_LLM_*/OPENAI_API_KEY)的优先级/默认/类型收敛。

P2 清扫补齐:legacy 族从 provider.py 内联散读并入 config 后,新旧解析的
逐字段+类型等价(7 组环境组合冒烟)在此冻结——oracle 为原 provider.py
内联语义的逐字转录(P2 前 HEAD 版本)。
"""

from __future__ import annotations

import os

import pytest

from stateforge.config import LegacyProviderSettings, llm_settings, \
    legacy_provider_settings

_FAMILY = [
    # legacy 族(两个 model 视图同读 SF_LLM_MODEL)
    "SF_LLM_PROVIDER", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL", "OPENAI_API_KEY", "SF_LLM_BASE_URL",
    "SF_LLM_MODEL", "SF_LLM_MAX_CALLS", "SF_LLM_SSL_VERIFY",
    # STATEFORGE_LLM_*(llm_settings 面,防宿主环境泄漏)
    "STATEFORGE_LLM_API_KEY", "STATEFORGE_LLM_BASE_URL",
    "STATEFORGE_LLM_MODEL", "STATEFORGE_LLM_TIMEOUT",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """清空全部相关环境变量:用例显式 setenv,宿主环境零泄漏。"""
    for name in _FAMILY:
        monkeypatch.delenv(name, raising=False)


# ------------------------------------------------------- llm_settings(STATEFORGE_LLM_*)


def test_llm_defaults():
    """无 env 无 overrides:默认值逐字=P1 前 RealLLMProvider 内置。"""
    cfg = llm_settings()
    assert cfg.api_key == ""                       # 空串=未配置 → 规则降级
    assert cfg.base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert cfg.model == "glm-4-flash"
    assert cfg.timeout == 30 and type(cfg.timeout) is int


def test_llm_env_beats_defaults(monkeypatch):
    monkeypatch.setenv("STATEFORGE_LLM_API_KEY", "k1")
    monkeypatch.setenv("STATEFORGE_LLM_BASE_URL", "http://host:8000/v1/")
    monkeypatch.setenv("STATEFORGE_LLM_MODEL", "m2")
    monkeypatch.setenv("STATEFORGE_LLM_TIMEOUT", "45")
    cfg = llm_settings()
    assert (cfg.api_key, cfg.model) == ("k1", "m2")
    assert cfg.base_url == "http://host:8000/v1"   # 尾部 / 统一去除
    assert cfg.timeout == 45 and type(cfg.timeout) is int


def test_llm_overrides_beat_env(monkeypatch):
    monkeypatch.setenv("STATEFORGE_LLM_API_KEY", "env-k")
    monkeypatch.setenv("STATEFORGE_LLM_MODEL", "env-m")
    monkeypatch.setenv("STATEFORGE_LLM_TIMEOUT", "45")
    cfg = llm_settings(api_key="ov-k", model="ov-m", timeout=1.5)
    assert (cfg.api_key, cfg.model) == ("ov-k", "ov-m")
    assert cfg.timeout == 1.5 and type(cfg.timeout) is float  # 数值 override 透传
    # override=None 显式回落 env
    assert llm_settings(api_key=None).api_key == "env-k"


def test_llm_timeout_int_coercion(monkeypatch):
    """字符串值(含环境变量)按 int 收敛;小数字符串抛 ValueError 不静默截断。"""
    with pytest.raises(ValueError):
        llm_settings(timeout="4.5")
    monkeypatch.setenv("STATEFORGE_LLM_TIMEOUT", "4.5")
    with pytest.raises(ValueError):
        llm_settings()                             # 环境变量路径同样抛
    monkeypatch.setenv("STATEFORGE_LLM_TIMEOUT", "60")
    assert llm_settings().timeout == 60            # env 整数字符串收敛
    assert llm_settings(timeout="12").timeout == 12  # override 字符串收敛


# ------------------------------------- legacy_provider_settings(ANTHROPIC_*/SF_LLM_*)


def test_legacy_defaults():
    """无 env:全部未配置=None(或分支缺省 model),max_calls/ssl_verify 内置。"""
    s = legacy_provider_settings()
    assert isinstance(s, LegacyProviderSettings)
    assert s.provider is None
    assert s.anthropic_api_key is None and s.anthropic_auth_token is None
    assert s.anthropic_base_url == "https://api.anthropic.com"
    assert s.openai_api_key is None and s.openai_base_url is None
    assert s.anthropic_model == "claude-sonnet-5"  # 同一 SF_LLM_MODEL 的两分支缺省
    assert s.openai_model == "glm-4.6"
    assert s.max_calls == 64 and type(s.max_calls) is int
    assert s.ssl_verify is True and type(s.ssl_verify) is bool


def test_legacy_env_resolution(monkeypatch):
    monkeypatch.setenv("SF_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://p.example.com///")
    monkeypatch.setenv("OPENAI_API_KEY", "ko")
    monkeypatch.setenv("SF_LLM_BASE_URL", "http://b:1/v1/")
    monkeypatch.setenv("SF_LLM_MAX_CALLS", "16")
    monkeypatch.setenv("SF_LLM_SSL_VERIFY", "false")
    s = legacy_provider_settings()
    assert (s.provider, s.anthropic_auth_token) == ("anthropic", "tok")
    assert s.anthropic_base_url == "https://p.example.com"   # 去尾 /
    # 注:openai_base_url 原样透传(含尾 /)——去尾 / 属 provider 工厂
    # (config 无缺省,保持 None,原 AttributeError 崩溃路径不挪位);
    # 工厂层去尾 / 由 test_legacy_provider_factory_equivalence 组合 4 冻结。
    assert (s.openai_api_key, s.openai_base_url) == ("ko", "http://b:1/v1/")
    assert s.max_calls == 16 and type(s.max_calls) is int
    assert s.ssl_verify is False
    # 同一 SF_LLM_MODEL:空串透传(仅"未配置"落分支缺省)——原 provider.py 语义
    monkeypatch.setenv("SF_LLM_MODEL", "")
    s2 = legacy_provider_settings()
    assert s2.anthropic_model == "" and s2.openai_model == ""
    monkeypatch.setenv("SF_LLM_MODEL", "m9")
    s3 = legacy_provider_settings()
    assert s3.anthropic_model == "m9" and s3.openai_model == "m9"
    # ANTHROPIC_BASE_URL 空串:原 `值 or 缺省` 语义 → 落缺省
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "")
    assert legacy_provider_settings().anthropic_base_url == "https://api.anthropic.com"


def test_legacy_overrides_beat_env(monkeypatch):
    monkeypatch.setenv("SF_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
    monkeypatch.setenv("SF_LLM_MAX_CALLS", "16")
    monkeypatch.setenv("SF_LLM_SSL_VERIFY", "false")
    s = legacy_provider_settings(provider="openai-compatible",
                                 anthropic_api_key="ov-key",
                                 max_calls="8", ssl_verify=True)
    assert (s.provider, s.anthropic_api_key) == ("openai-compatible", "ov-key")
    assert s.max_calls == 8 and s.ssl_verify is True


def test_legacy_max_calls_and_ssl_verify_coercion(monkeypatch):
    with pytest.raises(ValueError):
        legacy_provider_settings(max_calls="4.5")
    monkeypatch.setenv("SF_LLM_MAX_CALLS", "4.5")
    with pytest.raises(ValueError):
        legacy_provider_settings()                 # 环境变量路径同样抛
    monkeypatch.setenv("SF_LLM_MAX_CALLS", "7")
    assert legacy_provider_settings().max_calls == 7
    monkeypatch.setenv("SF_LLM_SSL_VERIFY", "FALSE")   # 仅精确 "false" 关(忽略大小写)
    assert legacy_provider_settings().ssl_verify is False
    monkeypatch.setenv("SF_LLM_SSL_VERIFY", "0")       # 非 "false" 一律 True
    assert legacy_provider_settings().ssl_verify is True


# ------------------------------------------- 新旧解析等价(oracle=原 provider.py 内联语义)


def _oracle_settings(env: dict[str, str]) -> dict:
    """P2 前 provider.py from_env/complete 内联读取的逐字转录(等价性 oracle)。"""

    def get(name: str, default: str | None = None) -> str | None:
        return env.get(name, default)

    explicit = get("SF_LLM_PROVIDER")
    return {
        "provider": explicit,
        "anthropic_api_key": get("ANTHROPIC_API_KEY"),
        "anthropic_auth_token": get("ANTHROPIC_AUTH_TOKEN"),
        "anthropic_base_url":
            (get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com").rstrip("/"),
        "openai_api_key": get("OPENAI_API_KEY"),
        "openai_base_url": get("SF_LLM_BASE_URL"),
        "anthropic_model": get("SF_LLM_MODEL", "claude-sonnet-5"),
        "openai_model": get("SF_LLM_MODEL", "glm-4.6"),
        "max_calls": int(get("SF_LLM_MAX_CALLS", "64")),
        "ssl_verify": get("SF_LLM_SSL_VERIFY", "true").lower() != "false",
    }


_SEVEN_COMBOS = [
    {},                                                             # 无任何配置
    {"ANTHROPIC_API_KEY": "sk-ant"},                                # key 直连
    {"ANTHROPIC_AUTH_TOKEN": "tok",
     "ANTHROPIC_BASE_URL": "https://p.example.com///"},             # 代理形态+去尾 /
    {"OPENAI_API_KEY": "sk-o", "SF_LLM_BASE_URL": "http://h:8/v1/"},  # openai 兼容
    {"SF_LLM_PROVIDER": "anthropic"},                               # 显式协议无凭据
    {"SF_LLM_PROVIDER": "openai-compatible",
     "SF_LLM_BASE_URL": "http://g:9/v1", "SF_LLM_MODEL": ""},       # 空串 model 透传
    {"SF_LLM_PROVIDER": "openai-compatible", "OPENAI_API_KEY": "k",
     "SF_LLM_BASE_URL": "http://b/", "SF_LLM_MODEL": "m9",
     "SF_LLM_MAX_CALLS": "7", "SF_LLM_SSL_VERIFY": "false",
     "ANTHROPIC_API_KEY": "also-set", "ANTHROPIC_BASE_URL": ""},    # 全开+空 base_url
]


def test_legacy_seven_combination_equivalence(monkeypatch):
    """7 组环境组合:legacy_provider_settings() vs oracle 逐字段+类型等价。"""
    for combo in _SEVEN_COMBOS:
        for name in _FAMILY:
            monkeypatch.delenv(name, raising=False)
        for name, val in combo.items():
            monkeypatch.setenv(name, val)
        env = {k: os.environ[k] for k in combo}
        got = legacy_provider_settings()
        want = _oracle_settings(env)
        for field, expected in want.items():
            actual = getattr(got, field)
            assert actual == expected, f"{combo} -> {field}: {actual!r} != {expected!r}"
            assert type(actual) is type(expected), \
                f"{combo} -> {field}: 类型 {type(actual).__name__} != {type(expected).__name__}"


def test_legacy_provider_factory_equivalence(monkeypatch):
    """同 7 组组合:Provider.from_env() 五元组(kind/base/headers/model/max_calls)
    与 oracle 工厂结果等价(新旧行为等价的最外层冒烟)。"""
    from stateforge.solving.llm.provider import Provider

    def oracle_provider(env: dict[str, str]):
        o = _oracle_settings(env)
        if o["provider"] == "anthropic" or (not o["provider"] and
                                            (o["anthropic_api_key"] or o["anthropic_auth_token"])):
            headers = (
                {"x-api-key": o["anthropic_api_key"], "anthropic-version": "2023-06-01"}
                if o["anthropic_api_key"] else
                {"Authorization": f"Bearer {o['anthropic_auth_token']}",
                 "anthropic-version": "2023-06-01"})
            return ("anthropic", o["anthropic_base_url"], headers,
                    o["anthropic_model"], o["max_calls"])
        if o["provider"] == "openai-compatible" or (not o["provider"]
                                                    and o["openai_api_key"] and o["openai_base_url"]):
            return ("openai-compatible", o["openai_base_url"].rstrip("/"),
                    {"Authorization": f"Bearer {o['openai_api_key']}"},
                    o["openai_model"], o["max_calls"])
        return None

    for combo in _SEVEN_COMBOS:
        for name in _FAMILY:
            monkeypatch.delenv(name, raising=False)
        for name, val in combo.items():
            monkeypatch.setenv(name, val)
        env = {k: os.environ[k] for k in combo}
        p = Provider.from_env()
        got = None if p is None else (
            p.kind, p.base_url, dict(p.headers), p.model, p.max_calls)
        assert got == oracle_provider(env), f"{combo} -> {got!r}"
