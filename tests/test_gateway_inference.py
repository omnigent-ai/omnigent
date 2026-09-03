from __future__ import annotations

import json
from typing import Any

import pytest

from omnigent import claude_native, codex_native_app_server, gateway_inference
from omnigent.claude_native import ClaudeNativeUcodeConfig
from omnigent.codex_native_app_server import NativeCodexLaunch, native_codex_launch_base_url
from omnigent.databricks_ai_gateway import is_databricks_ai_gateway_url
from omnigent.gateway_inference import (
    CLAUDE_GATEWAY_HARNESSES,
    CODEX_GATEWAY_HARNESSES,
    claude_gateway_inference_backed,
    codex_gateway_inference_backed,
    gateway_inference_map,
    gateway_inference_state,
    not_gateway_backed,
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


def _stub_managed_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    payload: dict[str, Any] | None,
) -> None:
    if payload is None:
        paths: tuple[Any, ...] = (tmp_path / "absent.json",)
    else:
        settings = tmp_path / "managed-settings.json"
        settings.write_text(json.dumps(payload), encoding="utf-8")
        paths = (settings,)
    monkeypatch.setattr(claude_native, "_CLAUDE_CODE_MANAGED_SETTINGS_PATHS", paths)


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


def test_claude_not_gateway_backed_for_resolved_non_databricks_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    _stub_claude(
        monkeypatch,
        ClaudeNativeUcodeConfig(
            env={"ANTHROPIC_BASE_URL": "https://api.anthropic.com"},
            api_key_helper="databricks auth token --profile dev",
        ),
    )
    _stub_managed_settings(monkeypatch, tmp_path, None)

    assert claude_gateway_inference_backed() is False


def test_claude_not_gateway_backed_without_api_key_helper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    _stub_claude(
        monkeypatch,
        ClaudeNativeUcodeConfig(env={"ANTHROPIC_BASE_URL": _GATEWAY_ANTHROPIC_URL}),
    )
    _stub_managed_settings(monkeypatch, tmp_path, None)

    assert claude_gateway_inference_backed() is False


def test_claude_not_gateway_backed_for_bedrock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    _stub_claude(
        monkeypatch,
        ClaudeNativeUcodeConfig(
            env={
                "ANTHROPIC_BEDROCK_BASE_URL": "https://bedrock-runtime.us-east-1.amazonaws.com",
                "CLAUDE_CODE_USE_BEDROCK": "1",
            },
        ),
    )
    _stub_managed_settings(monkeypatch, tmp_path, None)

    assert claude_gateway_inference_backed() is False


def test_claude_not_gateway_backed_for_cli_login(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    _stub_claude(monkeypatch, None)
    _stub_managed_settings(monkeypatch, tmp_path, None)

    assert claude_gateway_inference_backed() is False


def test_claude_gateway_backed_for_subscription_with_managed_helper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    _stub_claude(monkeypatch, None)
    _stub_managed_settings(
        monkeypatch,
        tmp_path,
        {
            "env": {"ANTHROPIC_BASE_URL": _GATEWAY_ANTHROPIC_URL},
            "apiKeyHelper": "jq -r '.access_token' ~/.databricks/model-serving-token.json",
        },
    )

    assert claude_gateway_inference_backed() is True


def test_claude_gateway_backed_for_subscription_with_use_gateway_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    _stub_claude(monkeypatch, None)
    _stub_managed_settings(
        monkeypatch,
        tmp_path,
        {
            "env": {
                "ANTHROPIC_BASE_URL": _GATEWAY_ANTHROPIC_URL,
                "CLAUDE_CODE_USE_GATEWAY": "1",
            }
        },
    )

    assert claude_gateway_inference_backed() is True


def test_claude_not_gateway_backed_for_managed_non_databricks_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    _stub_claude(monkeypatch, None)
    _stub_managed_settings(
        monkeypatch,
        tmp_path,
        {
            "env": {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"},
            "apiKeyHelper": "jq -r '.access_token' ~/.databricks/model-serving-token.json",
        },
    )

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


def test_codex_not_gateway_backed_for_cli_login(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _stub_codex_dismissed(monkeypatch, False)
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


def _write_codex_config(tmp_path: Any, providers_toml: str) -> None:
    (tmp_path / "config.toml").write_text(providers_toml, encoding="utf-8")


def test_codex_gateway_backed_for_cli_config_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _write_codex_config(
        tmp_path,
        f'[model_providers.Databricks]\nbase_url = "{_GATEWAY_CODEX_URL}"\n',
    )
    _stub_codex(
        monkeypatch,
        NativeCodexLaunch(
            config_overrides=['model_provider="Databricks"'], model=None, profile=None
        ),
    )

    assert codex_gateway_inference_backed() is True


def test_codex_not_gateway_backed_for_cli_config_non_databricks_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _write_codex_config(
        tmp_path,
        '[model_providers.Databricks]\nbase_url = "https://api.openai.com/v1"\n',
    )
    _stub_codex(
        monkeypatch,
        NativeCodexLaunch(
            config_overrides=['model_provider="Databricks"'], model=None, profile=None
        ),
    )

    assert codex_gateway_inference_backed() is False


def test_codex_not_gateway_backed_for_cli_config_missing_provider_table(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _write_codex_config(
        tmp_path,
        f'[model_providers.Other]\nbase_url = "{_GATEWAY_CODEX_URL}"\n',
    )
    _stub_codex(
        monkeypatch,
        NativeCodexLaunch(
            config_overrides=['model_provider="Databricks"'], model=None, profile=None
        ),
    )

    assert codex_gateway_inference_backed() is False


def _stub_codex_dismissed(monkeypatch: pytest.MonkeyPatch, dismissed: bool) -> None:
    from omnigent.onboarding import detected

    monkeypatch.setattr(detected, "codex_config_provider_dismissed", lambda _config: dismissed)


def test_codex_gateway_backed_for_config_default_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _stub_codex_dismissed(monkeypatch, False)
    _write_codex_config(
        tmp_path,
        f'model_provider = "Databricks"\n'
        f'[model_providers.Databricks]\nbase_url = "{_GATEWAY_CODEX_URL}"\n',
    )
    _stub_codex(
        monkeypatch,
        NativeCodexLaunch(config_overrides=[], model=None, profile=None),
    )

    assert codex_gateway_inference_backed() is True


def test_codex_not_gateway_backed_for_config_default_when_dismissed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _stub_codex_dismissed(monkeypatch, True)
    _write_codex_config(
        tmp_path,
        f'model_provider = "Databricks"\n'
        f'[model_providers.Databricks]\nbase_url = "{_GATEWAY_CODEX_URL}"\n',
    )
    _stub_codex(
        monkeypatch,
        NativeCodexLaunch(config_overrides=[], model=None, profile=None),
    )

    assert codex_gateway_inference_backed() is False


def test_codex_not_gateway_backed_for_explicit_openai_ignores_config_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _stub_codex_dismissed(monkeypatch, False)
    _write_codex_config(
        tmp_path,
        f'model_provider = "Databricks"\n'
        f'[model_providers.Databricks]\nbase_url = "{_GATEWAY_CODEX_URL}"\n',
    )
    _stub_codex(
        monkeypatch,
        NativeCodexLaunch(config_overrides=['model_provider="openai"'], model=None, profile=None),
    )

    assert codex_gateway_inference_backed() is False


def test_codex_not_gateway_backed_for_config_default_non_databricks_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _stub_codex_dismissed(monkeypatch, False)
    _write_codex_config(
        tmp_path,
        'model_provider = "OpenAI"\n[model_providers.OpenAI]\nbase_url = "https://api.openai.com/v1"\n',
    )
    _stub_codex(
        monkeypatch,
        NativeCodexLaunch(config_overrides=[], model=None, profile=None),
    )

    assert codex_gateway_inference_backed() is False


def test_codex_not_gateway_backed_for_config_default_without_top_level_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _stub_codex_dismissed(monkeypatch, False)
    _write_codex_config(
        tmp_path,
        f'[model_providers.Databricks]\nbase_url = "{_GATEWAY_CODEX_URL}"\n',
    )
    _stub_codex(
        monkeypatch,
        NativeCodexLaunch(config_overrides=[], model=None, profile=None),
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


def test_launch_base_url_none_for_cli_config_provider_name_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
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


# ── Reading a reported map back ─────────────────────────────────────────────
#
# The server and the CLI both hold a harness id and a host's reported map, and
# both must tell "not gateway-backed" apart from "the host said nothing".


@pytest.mark.parametrize(
    ("gateway", "harness", "expected"),
    [
        ({"codex-native": False}, "codex-native", False),
        ({"codex-native": True}, "codex-native", True),
        # Any spelling of the family finds the family's entry, including the
        # reversed aliases that never canonicalize back.
        ({"codex": False}, "native-codex", False),
        ({"native-claude": True}, "claude-native", True),
        # Unknown: an absent entry, an absent map, and a bare family name (never
        # a key the host emits) all read as "could not tell".
        ({"claude-native": True}, "codex-native", None),
        (None, "codex-native", None),
        ({}, "codex-native", None),
        ({"claude": False}, "claude-native", None),
        # A harness in neither family has no fan-out to fall back on.
        ({"pi-native": False}, "pi-native", False),
        ({"pi-native": False}, "cursor-native", None),
    ],
)
def test_gateway_inference_state_reads_any_spelling_of_the_family(
    gateway: dict[str, Any] | None,
    harness: str,
    expected: bool | None,
) -> None:
    assert gateway_inference_state(gateway, harness) is expected


def test_not_gateway_backed_names_only_the_explicit_false_arms() -> None:
    arms = ("claude-native", "codex-native")
    # Claude Code on the AI Gateway, Codex on a personal ChatGPT subscription.
    assert not_gateway_backed({"claude-native": True, "codex-native": False}, arms) == [
        "codex-native"
    ]
    assert not_gateway_backed({"claude-native": False, "codex-native": False}, arms) == list(arms)
    assert not_gateway_backed({"claude-native": True, "codex-native": True}, arms) == []
    # Unknown keeps every option: an older host must not silently lose routing.
    assert not_gateway_backed(None, arms) == []
    assert not_gateway_backed({"claude-native": True}, arms) == []


@pytest.mark.parametrize(
    "gateway_url",
    [
        # Canonical dedicated-subdomain gateway, and the staging variant.
        "https://wkspc.ai-gateway.cloud.databricks.com/codex/v1",
        "https://wkspc.ai-gateway.staging.cloud.databricks.com/codex/v1",
        # Azure / GCP parent domains.
        "https://wkspc.ai-gateway.azuredatabricks.net/codex/v1",
        "https://wkspc.ai-gateway.gcp.databricks.com/codex/v1",
        # Workspace-hosted shape: no ai-gateway label, but the path prefix.
        "https://wkspc.cloud.databricks.com/ai-gateway/codex/v1",
    ],
)
def test_gateway_url_accepts_databricks_owned_hosts(gateway_url: str) -> None:
    assert is_databricks_ai_gateway_url(gateway_url) is True


@pytest.mark.parametrize(
    "gateway_url",
    [
        # The trusted domain appears mid-host; .evil.test really owns it.
        "https://x.ai-gateway.cloud.databricks.com.evil.test/codex/v1",
        # Label-boundary look-alikes: a string-suffix test on the bare parent
        # domain (no leading dot) would accept both of these.
        "https://evilcloud.databricks.com/ai-gateway/codex/v1",
        "https://ai-gateway.notazuredatabricks.net/codex/v1",
        # The parent domain itself is not a workspace subdomain.
        "https://cloud.databricks.com/ai-gateway/codex/v1",
        # Trusted host, but neither gateway shape (no label, no path prefix).
        "https://wkspc.cloud.databricks.com/codex/v1",
        # ai-gateway only as part of a label.
        "https://my-ai-gateway-proxy.cloud.databricks.com/codex/v1",
        # Right host, plaintext http — the bearer must never go over http.
        "http://wkspc.ai-gateway.cloud.databricks.com/codex/v1",
        # Both markers in the path of a foreign host.
        "https://evil.test/databricks/ai-gateway/codex/v1",
        "not-a-url",
        "",
    ],
)
def test_gateway_url_rejects_lookalike_hosts(gateway_url: str) -> None:
    assert is_databricks_ai_gateway_url(gateway_url) is False


def test_introspection_does_not_narrate_launch_routing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Resolving a launch to inspect it must not log as if it launched.

    A long-lived host daemon re-runs these checks every minute, so narrating
    each one buried the host's own lifecycle lines under thousands of
    identical routing lines — 98% of one 7-hour daemon log.
    """
    import logging

    resolver = logging.getLogger("omnigent.claude_native")

    def _resolve_and_narrate(**kwargs: Any) -> ClaudeNativeUcodeConfig:
        # Stand in for the real resolver, which narrates the routing it picked.
        resolver.info("native-claude routing: provider 'gw' (base_url=%s)", _GATEWAY_ANTHROPIC_URL)
        return ClaudeNativeUcodeConfig(
            env={"ANTHROPIC_BASE_URL": _GATEWAY_ANTHROPIC_URL},
            api_key_helper="databricks auth token --profile dev",
        )

    monkeypatch.setattr(claude_native, "resolve_native_claude_config", _resolve_and_narrate)

    with caplog.at_level(logging.INFO):
        assert claude_gateway_inference_backed() is True

    routing = [r.getMessage() for r in caplog.records if "routing" in r.getMessage()]
    assert routing == [], f"introspection must not narrate routing, got {routing}"


def test_a_real_launch_still_narrates_its_routing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The filter must not become a blanket mute on the resolver's logger.

    The routing line is exactly what an operator wants when a session really
    launches; only the repeated introspection is noise.
    """
    import logging

    resolver = logging.getLogger("omnigent.claude_native")
    _stub_claude(
        monkeypatch,
        ClaudeNativeUcodeConfig(
            env={"ANTHROPIC_BASE_URL": _GATEWAY_ANTHROPIC_URL},
            api_key_helper="databricks auth token --profile dev",
        ),
    )

    with caplog.at_level(logging.INFO):
        # Install the filter via one introspection pass, then log as a launch would.
        assert claude_gateway_inference_backed() is True
        resolver.info("native-claude routing: provider 'gw' (real launch)")

    routing = [r.getMessage() for r in caplog.records if "routing" in r.getMessage()]
    assert routing == ["native-claude routing: provider 'gw' (real launch)"]


def test_narration_survives_a_launch_on_another_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Suppression is scoped to the introspecting thread.

    Host launches run as tasks on the daemon's event loop while these checks
    run in a worker thread, so a process-wide mute would swallow the routing
    line of a session that really did launch.
    """
    import logging
    import threading

    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    resolver = logging.getLogger("omnigent.claude_native")
    handler = _Capture()
    resolver.addHandler(handler)
    resolver.setLevel(logging.INFO)
    started = threading.Event()

    def _concurrent_launch() -> None:
        started.wait(timeout=5)
        resolver.info("native-claude routing: concurrent launch")

    try:
        with gateway_inference._quiet_resolver_narration():
            thread = threading.Thread(target=_concurrent_launch)
            thread.start()
            started.set()
            thread.join(timeout=5)
            resolver.info("native-claude routing: suppressed on this thread")
    finally:
        resolver.removeHandler(handler)

    assert records == ["native-claude routing: concurrent launch"]


def test_nested_introspection_restores_narration(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Leaving a nested check does not un-suppress its caller."""
    import logging

    resolver = logging.getLogger("omnigent.claude_native")

    with caplog.at_level(logging.INFO):
        with gateway_inference._quiet_resolver_narration():
            with gateway_inference._quiet_resolver_narration():
                pass
            resolver.info("native-claude routing: still inside")
        resolver.info("native-claude routing: after")

    messages = [r.getMessage() for r in caplog.records if "routing" in r.getMessage()]
    assert messages == ["native-claude routing: after"]
