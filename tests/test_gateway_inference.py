from __future__ import annotations

from typing import Any

import pytest

from omnigent import claude_native, codex_native_app_server, gateway_inference
from omnigent.claude_native import ClaudeNativeUcodeConfig
from omnigent.codex_native_app_server import NativeCodexLaunch, native_codex_launch_base_url
from omnigent.gateway_inference import (
    CLAUDE_GATEWAY_HARNESSES,
    CODEX_GATEWAY_HARNESSES,
    claude_gateway_inference_backed,
    codex_gateway_inference_backed,
    gateway_inference_map,
)
from omnigent.inner import codex_executor

_GATEWAY_CODEX_URL = "https://example.cloud.databricks.com/ai-gateway/codex/v1"
_GATEWAY_ANTHROPIC_URL = "https://example.cloud.databricks.com/ai-gateway/anthropic"


def _stub_claude(
    monkeypatch: pytest.MonkeyPatch,
    config: ClaudeNativeUcodeConfig | None,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _resolve(**kwargs: Any) -> ClaudeNativeUcodeConfig | None:
        calls.append(kwargs)
        return config

    monkeypatch.setattr(claude_native, "resolve_native_claude_config", _resolve)
    return calls


def _stub_codex(monkeypatch: pytest.MonkeyPatch, launch: NativeCodexLaunch) -> None:
    monkeypatch.setattr(
        codex_native_app_server,
        "resolve_native_codex_launch",
        lambda **_kwargs: launch,
    )


def test_claude_gateway_backed_for_gateway_env_with_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_claude(
        monkeypatch,
        ClaudeNativeUcodeConfig(
            env={"ANTHROPIC_BASE_URL": _GATEWAY_ANTHROPIC_URL},
            api_key_helper="databricks auth token --profile dev",
        ),
    )

    assert claude_gateway_inference_backed() is True
    assert calls == [{"spec": None, "refresh_models": False}]


def test_claude_not_gateway_backed_without_api_key_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_claude(
        monkeypatch,
        ClaudeNativeUcodeConfig(env={"ANTHROPIC_BASE_URL": _GATEWAY_ANTHROPIC_URL}),
    )

    assert claude_gateway_inference_backed() is False


def test_claude_not_gateway_backed_for_bedrock(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_claude(
        monkeypatch,
        ClaudeNativeUcodeConfig(
            env={
                "ANTHROPIC_BEDROCK_BASE_URL": "https://bedrock-runtime.us-east-1.amazonaws.com",
                "CLAUDE_CODE_USE_BEDROCK": "1",
            },
        ),
    )

    assert claude_gateway_inference_backed() is False


def test_claude_not_gateway_backed_for_cli_login(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_claude(monkeypatch, None)

    assert claude_gateway_inference_backed() is False


def test_codex_gateway_backed_for_databricks_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        codex_native_app_server,
        "_databricks_gateway_host",
        lambda _profile: "https://example.cloud.databricks.com/",
    )
    _stub_codex(
        monkeypatch,
        NativeCodexLaunch(config_overrides=[], model=None, profile="dev"),
    )

    assert codex_gateway_inference_backed() is True


def test_codex_not_gateway_backed_for_non_databricks_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overrides = codex_executor._provider_codex_config_overrides(
        model="qwen/qwen3.7-plus",
        base_url="https://openrouter.ai/api/v1",
        auth_command="printf %s sk-test",
        wire_api="chat",
    )
    _stub_codex(
        monkeypatch,
        NativeCodexLaunch(config_overrides=overrides, model=None, profile=None),
    )

    assert codex_gateway_inference_backed() is False


def test_codex_not_gateway_backed_for_cli_login(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_codex(
        monkeypatch,
        NativeCodexLaunch(config_overrides=[], model=None, profile=None),
    )

    assert codex_gateway_inference_backed() is False


def test_codex_not_gateway_backed_when_profile_has_no_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_native_app_server, "_databricks_gateway_host", lambda _profile: None)
    _stub_codex(
        monkeypatch,
        NativeCodexLaunch(config_overrides=[], model=None, profile="dev"),
    )

    assert codex_gateway_inference_backed() is False


def test_launch_base_url_extracts_generated_databricks_override() -> None:
    overrides = codex_executor._databricks_codex_config_overrides(
        model="databricks-gpt-5-5",
        base_url=_GATEWAY_CODEX_URL,
        auth_command="databricks auth token --profile dev",
    )
    launch = NativeCodexLaunch(config_overrides=overrides, model=None, profile=None)

    assert native_codex_launch_base_url(launch) == _GATEWAY_CODEX_URL


def test_launch_base_url_none_for_cli_config_provider_name_only() -> None:
    launch = NativeCodexLaunch(
        config_overrides=['model_provider="my_custom"'],
        model=None,
        profile=None,
    )

    assert native_codex_launch_base_url(launch) is None


def test_codex_not_gateway_backed_for_non_codex_gateway_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overrides = codex_executor._provider_codex_config_overrides(
        model=None,
        base_url=_GATEWAY_ANTHROPIC_URL,
        auth_command="printf %s token",
        wire_api="responses",
    )
    _stub_codex(
        monkeypatch,
        NativeCodexLaunch(config_overrides=overrides, model=None, profile=None),
    )

    assert codex_gateway_inference_backed() is False


def test_gateway_inference_map_fans_out_over_every_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway_inference, "claude_gateway_inference_backed", lambda: True)
    monkeypatch.setattr(gateway_inference, "codex_gateway_inference_backed", lambda: False)

    result = gateway_inference_map()

    assert result == {
        **dict.fromkeys(CLAUDE_GATEWAY_HARNESSES, True),
        **dict.fromkeys(CODEX_GATEWAY_HARNESSES, False),
    }
    assert set(result) == {
        "claude-native",
        "native-claude",
        "codex",
        "codex-native",
        "native-codex",
    }


def test_gateway_inference_map_omits_a_family_whose_check_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> bool:
        raise RuntimeError("no databricks config")

    monkeypatch.setattr(gateway_inference, "claude_gateway_inference_backed", _boom)
    monkeypatch.setattr(gateway_inference, "codex_gateway_inference_backed", lambda: True)

    result = gateway_inference_map()

    assert result == dict.fromkeys(CODEX_GATEWAY_HARNESSES, True)
    assert not any(spelling in result for spelling in CLAUDE_GATEWAY_HARNESSES)
