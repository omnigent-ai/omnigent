"""Tests for omnigent.pi_native_credentials (native Pi provider wiring)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from omnigent import pi_native_credentials as creds


def _databricks_config() -> dict[str, object]:
    """A config whose default provider is a Databricks profile (serves pi)."""
    return {
        "providers": {
            "databricks": {"kind": "databricks", "default": True, "profile": "demo-staging"},
        }
    }


def test_resolves_databricks_default_to_anthropic_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Databricks default → Pi anthropic-messages gateway provider.

    The Databricks profile is marked default for the anthropic/openai surfaces
    (not ``pi`` directly), so the resolver must fall back to the Anthropic
    surface — which Pi speaks natively — and build a gateway provider with a
    bearer-token refresh command.
    """
    from omnigent.inner import databricks_executor

    def _host(profile: str | None) -> str:
        return "https://wkspc.example.com/"

    monkeypatch.setattr(databricks_executor, "_read_databrickscfg_host", _host)

    provider = creds.resolve_pi_native_provider(config_loader=_databricks_config)

    assert provider is not None
    assert provider.api == "anthropic-messages"
    assert provider.base_url == "https://wkspc.example.com/ai-gateway/anthropic"
    assert provider.model == "databricks-claude-sonnet-4-6"
    assert provider.auth_header is True
    # apiKey is a "!command" so Pi refreshes the gateway token per request.
    assert provider.api_key.startswith("!")
    assert "demo-staging" in provider.api_key


def test_databricks_unresolvable_host_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """No host for the profile → fall back to Pi's own login (None)."""
    from omnigent.inner import databricks_executor

    def _no_host(profile: str | None) -> None:
        return None

    monkeypatch.setattr(databricks_executor, "_read_databrickscfg_host", _no_host)
    assert creds.resolve_pi_native_provider(config_loader=_databricks_config) is None


def test_key_provider_resolves_to_inline_family() -> None:
    """A key-kind provider with an anthropic family → inline Pi provider."""
    config = {
        "providers": {
            "anthropic": {
                "kind": "key",
                "default": True,
                "anthropic": {
                    "base_url": "https://api.anthropic.com",
                    "api_key": "sk-test-literal",
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(
        model="claude-sonnet-4-6", config_loader=lambda: config
    )
    assert provider is not None
    assert provider.api == "anthropic-messages"
    assert provider.base_url == "https://api.anthropic.com"
    assert provider.api_key == "sk-test-literal"
    assert provider.auth_header is False
    assert provider.model == "claude-sonnet-4-6"


def test_subscription_default_returns_none() -> None:
    """A subscription (CLI-login) default isn't reusable by Pi → None."""
    config = {"providers": {"claude": {"kind": "subscription", "default": True, "cli": "claude"}}}
    assert creds.resolve_pi_native_provider(config_loader=lambda: config) is None


def test_no_providers_returns_none() -> None:
    """No configured providers → None (Pi uses its own login)."""
    assert creds.resolve_pi_native_provider(config_loader=dict) is None


def test_malformed_config_returns_none() -> None:
    """A loader that raises must not break launch — resolve to None."""

    def _boom() -> dict[str, object]:
        raise RuntimeError("bad config")

    assert creds.resolve_pi_native_provider(config_loader=_boom) is None


def test_unresolvable_secret_falls_back_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider whose secret can't resolve → None, not a hard launch failure.

    A key-kind default whose ``api_key`` references an env var absent from the
    runner env makes ``entry.family()`` raise during resolution (not during the
    config load). The contract is "any resolution failure → fall back to Pi's
    own login", so the resolver must swallow it and return ``None`` rather than
    let the exception fail the Pi terminal launch.
    """
    monkeypatch.delenv("PI_NATIVE_AUDIT_UNSET_KEY", raising=False)
    config = {
        "providers": {
            "anthropic": {
                "kind": "key",
                "default": True,
                "anthropic": {
                    "base_url": "https://api.anthropic.com",
                    "api_key": "$PI_NATIVE_AUDIT_UNSET_KEY",
                },
            }
        }
    }
    assert creds.resolve_pi_native_provider(config_loader=lambda: config) is None


def test_to_models_config_shape() -> None:
    """The rendered models.json carries baseUrl/api/apiKey/models (+authHeader)."""
    provider = creds.PiProviderConfig(
        provider_id="omnigent",
        base_url="https://x/ai-gateway/anthropic",
        api="anthropic-messages",
        model="databricks-claude-sonnet-4-6",
        api_key="!get-token",
        auth_header=True,
    )
    cfg = provider.to_models_config()
    entry = cfg["providers"]["omnigent"]
    assert entry["baseUrl"] == "https://x/ai-gateway/anthropic"
    assert entry["api"] == "anthropic-messages"
    assert entry["apiKey"] == "!get-token"
    assert entry["authHeader"] is True
    assert entry["models"] == [{"id": "databricks-claude-sonnet-4-6"}]


def test_write_models_config_is_owner_only(tmp_path: Path) -> None:
    """models.json is written 0600 in a 0700 dir (it may hold a literal key)."""
    provider = creds.PiProviderConfig(
        provider_id="omnigent",
        base_url="https://api.anthropic.com",
        api="anthropic-messages",
        model="claude-sonnet-4-6",
        api_key="sk-secret",
        auth_header=False,
    )
    agent_dir = tmp_path / "pi-agent"
    path = creds.write_pi_models_config(agent_dir, provider)

    assert path == agent_dir / "models.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(agent_dir.stat().st_mode) == 0o700
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["providers"]["omnigent"]["apiKey"] == "sk-secret"


def test_provider_launch_returns_env_and_args(tmp_path: Path) -> None:
    """pi_native_provider_launch writes config and returns the env + CLI args."""
    provider = creds.PiProviderConfig(
        provider_id="omnigent",
        base_url="https://api.anthropic.com",
        api="anthropic-messages",
        model="claude-sonnet-4-6",
        api_key="sk-secret",
        auth_header=False,
    )
    agent_dir = tmp_path / "pi-agent"
    env, args = creds.pi_native_provider_launch(agent_dir, provider)

    assert env == {creds.PI_CODING_AGENT_DIR_ENV_VAR: str(agent_dir)}
    assert args == ["--provider", "omnigent", "--model", "claude-sonnet-4-6"]
    assert (agent_dir / "models.json").exists()


def test_openai_chat_wire_api_resolves_to_completions(monkeypatch: pytest.MonkeyPatch) -> None:
    """An OpenAI family with wire_api: chat → openai-completions API.

    This tests the fix for the DeepInfra bug where pi-native was ignoring
    the wire_api setting and always using openai-responses. Providers like
    DeepInfra implement Chat Completions (/v1/openai/chat/completions) but
    not the Responses API (/v1/openai/responses returns 404).
    """
    # Set a fake API key in the environment for testing
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-deepinfra-key")

    config = {
        "providers": {
            "deepinfra": {
                "kind": "gateway",
                "default": True,
                "openai": {
                    "base_url": "https://api.deepinfra.com/v1/openai",
                    "api_key": "$OPENAI_API_KEY",
                    "wire_api": "chat",
                    "models": {"default": "zai-org/GLM-4.7"},
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(config_loader=lambda: config)
    assert provider is not None
    # wire_api: chat should resolve to openai-completions, not openai-responses
    assert provider.api == "openai-completions", (
        f"Expected openai-completions but got {provider.api} "
        f"(wire_api:chat should use chat completions API, not responses)"
    )
    assert provider.base_url == "https://api.deepinfra.com/v1/openai"
    assert provider.model == "zai-org/GLM-4.7"
    assert provider.api_key == "sk-test-deepinfra-key"  # Resolved from environment
    assert provider.auth_header is False


def test_openai_responses_wire_api_default() -> None:
    """An OpenAI family without wire_api (or wire_api: responses) → openai-responses API.

    When wire_api is not set or set to "responses", the default behavior
    should be to use the OpenAI Responses API.
    """
    config = {
        "providers": {
            "openai-gateway": {
                "kind": "gateway",
                "default": True,
                "openai": {
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "sk-test",
                    "models": {"default": "gpt-4o"},
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(config_loader=lambda: config)
    assert provider is not None
    # Default (no wire_api) should use openai-responses
    assert provider.api == "openai-responses"
    assert provider.base_url == "https://api.openai.com/v1"
    assert provider.model == "gpt-4o"


def test_openai_responses_wire_api_explicit() -> None:
    """An OpenAI family with wire_api: responses → openai-responses API.

    When wire_api is explicitly set to "responses", it should use the
    OpenAI Responses API.
    """
    config = {
        "providers": {
            "openai-gateway": {
                "kind": "gateway",
                "default": True,
                "openai": {
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "sk-test",
                    "wire_api": "responses",
                    "models": {"default": "gpt-4o"},
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(config_loader=lambda: config)
    assert provider is not None
    # Explicit wire_api: responses should use openai-responses
    assert provider.api == "openai-responses"
    assert provider.base_url == "https://api.openai.com/v1"
    assert provider.model == "gpt-4o"


def _cli_config_databricks_config() -> dict[str, object]:
    """A config whose default is a cli-config Databricks gateway (openai surface)."""
    return {
        "providers": {
            "codex-databricks": {
                "kind": "cli-config",
                "default": True,
                "cli": "codex",
                "model_provider": "Databricks",
                "display_name": "Databricks AI Gateway",
            },
        }
    }


def _write_codex_config(home: Path, body: str) -> None:
    """Write a ``~/.codex/config.toml`` under *home* (the resolver reads $HOME)."""
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)
    (codex_dir / "config.toml").write_text(body, encoding="utf-8")


_DATABRICKS_CODEX_CONFIG = """
model_provider = "Databricks"

[model_providers.Databricks]
name = "Databricks AI Gateway"
base_url = "https://1965859176160743.ai-gateway.cloud.databricks.com/codex/v1"
wire_api = "responses"

[model_providers.Databricks.auth]
command = "jq"
args = ["-r", ".access_token", "/Users/me/.databricks/model-serving-token.json"]
timeout_ms = 5000
"""


def test_cli_config_databricks_resolves_to_anthropic_gateway(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A cli-config Databricks default → Pi anthropic-messages gateway provider.

    The bug this fixes: previously the resolver returned ``None`` for
    ``cli-config``, silently dropping Pi to its own login. Now it reads the
    transport (base_url + auth command) from the pinned ``[model_providers.X]``
    table in ``~/.codex/config.toml``, rewrites the Codex base URL to the
    gateway's Anthropic surface, and emits a ``!command`` apiKey.
    """
    _write_codex_config(tmp_path, _DATABRICKS_CODEX_CONFIG)
    monkeypatch.setenv("HOME", str(tmp_path))

    provider = creds.resolve_pi_native_provider(config_loader=_cli_config_databricks_config)

    assert provider is not None
    assert provider.api == "anthropic-messages"
    # /codex/v1 rewritten to the /anthropic surface Pi speaks natively.
    assert (
        provider.base_url == "https://1965859176160743.ai-gateway.cloud.databricks.com/anthropic"
    )
    assert provider.model == "databricks-claude-sonnet-4-6"
    assert provider.auth_header is True
    # apiKey is a "!command" rebuilt from the table's [X.auth] command + args
    # so Pi refreshes the gateway token per request.
    assert provider.api_key == (
        "!jq -r .access_token /Users/me/.databricks/model-serving-token.json"
    )


def test_cli_config_databricks_respects_model_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A session model override wins over the cli-config Databricks default."""
    _write_codex_config(tmp_path, _DATABRICKS_CODEX_CONFIG)
    monkeypatch.setenv("HOME", str(tmp_path))

    provider = creds.resolve_pi_native_provider(
        model="databricks-claude-opus-4-8",
        config_loader=_cli_config_databricks_config,
    )
    assert provider is not None
    assert provider.model == "databricks-claude-opus-4-8"
    assert (
        provider.base_url == "https://1965859176160743.ai-gateway.cloud.databricks.com/anthropic"
    )


def test_cli_config_missing_codex_table_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A cli-config entry whose codex table is absent → None (graceful fallback)."""
    # config.toml exists but defines no [model_providers.Databricks] table.
    _write_codex_config(tmp_path, 'model_provider = "Databricks"\n')
    monkeypatch.setenv("HOME", str(tmp_path))
    assert creds.resolve_pi_native_provider(config_loader=_cli_config_databricks_config) is None


def test_cli_config_non_databricks_gateway_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A cli-config provider that is NOT a Databricks gateway → None.

    Gateway detection is by base_url shape (``*.ai-gateway.*databricks*``), so a
    generic custom provider pointing elsewhere falls back to Pi's own login
    rather than being mistranslated as the Databricks Anthropic surface.
    """
    _write_codex_config(
        tmp_path,
        """
model_provider = "Databricks"

[model_providers.Databricks]
name = "Some Other Proxy"
base_url = "https://proxy.example.com/v1"

[model_providers.Databricks.auth]
command = "printf"
args = ["%s", "sk-static"]
""",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    assert creds.resolve_pi_native_provider(config_loader=_cli_config_databricks_config) is None


def test_cli_config_databricks_warns_on_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unresolvable cli-config Databricks logs a clear reason (not silent)."""
    _write_codex_config(tmp_path, 'model_provider = "Databricks"\n')
    monkeypatch.setenv("HOME", str(tmp_path))
    import logging

    with caplog.at_level(logging.INFO, logger="omnigent.pi_native_credentials"):
        assert (
            creds.resolve_pi_native_provider(config_loader=_cli_config_databricks_config) is None
        )
    assert any("codex-databricks" in rec.getMessage() for rec in caplog.records)


def _codex_config_with_base_url(base_url: str) -> str:
    """A codex config.toml whose Databricks table points at *base_url*."""
    return f"""
model_provider = "Databricks"

[model_providers.Databricks]
name = "Databricks AI Gateway"
base_url = "{base_url}"
wire_api = "responses"

[model_providers.Databricks.auth]
command = "jq"
args = ["-r", ".access_token", "/Users/me/.databricks/model-serving-token.json"]
timeout_ms = 5000
"""


# Look-alike base URLs from the security finding: each embeds the "databricks"
# and "ai-gateway" substrings somewhere in scheme+host+path, defeating the old
# substring scan, but NONE is a real Databricks AI Gateway host. Routing any of
# them would leak the workspace bearer token to an attacker-controlled host.
_LOOKALIKE_GATEWAY_URLS = [
    # "ai-gateway" + "databricks" labels, but the real host is evil.test.
    "https://databricks-ai-gateway.evil.test/codex/v1",
    # Trusted suffix appears mid-host; the actual parent domain is .evil.test.
    "https://x.ai-gateway.cloud.databricks.com.evil.test/codex/v1",
    # Both substrings live in the path, not the host.
    "https://evil.test/databricks/ai-gateway/v1",
    # Right host shape but plaintext http (token must never go over http).
    "http://1965859176160743.ai-gateway.cloud.databricks.com/codex/v1",
]


@pytest.mark.parametrize("gateway_url", _LOOKALIKE_GATEWAY_URLS)
def test_cli_config_lookalike_gateway_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gateway_url: str
) -> None:
    """A look-alike (non-Databricks) gateway URL → None, never forwards the token.

    The old detector matched the "databricks" and "ai-gateway" substrings
    anywhere in the full base_url, so these look-alikes all passed and the code
    would emit the workspace bearer token as the apiKey for an attacker host.
    The hardened detector parses the URL and validates the *hostname* against a
    trusted Databricks domain suffix allowlist, so each falls back to Pi login.
    """
    _write_codex_config(tmp_path, _codex_config_with_base_url(gateway_url))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert creds.resolve_pi_native_provider(config_loader=_cli_config_databricks_config) is None


def test_real_gateway_still_resolves_after_hardening(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The proven real gateway URL still resolves end-to-end after hardening.

    Guards against over-tightening: the canonical
    ``<workspace>.ai-gateway.cloud.databricks.com`` host must still translate to
    the Anthropic surface with the ``!command`` apiKey.
    """
    _write_codex_config(
        tmp_path,
        _codex_config_with_base_url(
            "https://1965859176160743.ai-gateway.cloud.databricks.com/codex/v1"
        ),
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    provider = creds.resolve_pi_native_provider(config_loader=_cli_config_databricks_config)

    assert provider is not None
    assert (
        provider.base_url == "https://1965859176160743.ai-gateway.cloud.databricks.com/anthropic"
    )
    assert provider.api == "anthropic-messages"
    assert provider.api_key == (
        "!jq -r .access_token /Users/me/.databricks/model-serving-token.json"
    )


# ── Cross-surface selection: a cli-config Databricks gateway must be reachable
#    and selectable for pi (the bug: the old pi filter excluded all cli-config) ──


def _cli_config_databricks_pinned_pi() -> dict[str, object]:
    """A config where the cli-config Databricks gateway is pinned ``default: [openai, pi]``.

    Alongside an anthropic key that defaults only the anthropic surface, the
    Databricks gateway explicitly claims the pi scope — which the parser now
    accepts for a Databricks cli-config gateway. ``resolve_pi_native_provider``
    must select the gateway (its explicit pi default wins the shared
    selection), NOT api.anthropic.com.
    """
    return {
        "providers": {
            "anthropic": {
                "kind": "key",
                "default": "anthropic",
                "anthropic": {
                    "base_url": "https://api.anthropic.com",
                    "api_key": "sk-test-literal",
                },
            },
            "codex-databricks": {
                "kind": "cli-config",
                "default": ["openai", "pi"],
                "cli": "codex",
                "model_provider": "Databricks",
                "display_name": "Databricks AI Gateway",
            },
        }
    }


def test_explicit_pi_pin_selects_cli_config_databricks_over_anthropic_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit ``default: pi`` on a cli-config Databricks gateway wins for pi.

    Even with an anthropic key present (its own anthropic-surface default), the
    Databricks gateway pinned to the pi scope must be the pi selection — proving
    the parser accepts ``default: [openai, pi]`` for a Databricks cli-config AND
    the shared selection routes pi to it (base_url is the gateway's /anthropic
    surface, NOT api.anthropic.com).
    """
    _write_codex_config(tmp_path, _DATABRICKS_CODEX_CONFIG)
    monkeypatch.setenv("HOME", str(tmp_path))

    provider = creds.resolve_pi_native_provider(config_loader=_cli_config_databricks_pinned_pi)

    assert provider is not None
    assert (
        provider.base_url == "https://1965859176160743.ai-gateway.cloud.databricks.com/anthropic"
    )
    assert provider.api == "anthropic-messages"
    assert provider.auth_header is True
    assert provider.api_key == (
        "!jq -r .access_token /Users/me/.databricks/model-serving-token.json"
    )
    # NOT the anthropic key endpoint.
    assert provider.base_url != "https://api.anthropic.com"


def test_cli_config_databricks_as_sole_default_selected_for_pi(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A cli-config Databricks gateway as the only openai default is selected for pi.

    No explicit pi default and no anthropic default: the shared pi fallback
    reaches the openai default, and because it is a pi-consumable Databricks
    gateway, selection no longer skips it (the bug: the old filter excluded all
    cli-config from pi). Pi routes to the gateway's /anthropic surface.
    """
    _write_codex_config(tmp_path, _DATABRICKS_CODEX_CONFIG)
    monkeypatch.setenv("HOME", str(tmp_path))

    provider = creds.resolve_pi_native_provider(config_loader=_cli_config_databricks_config)

    assert provider is not None
    assert (
        provider.base_url == "https://1965859176160743.ai-gateway.cloud.databricks.com/anthropic"
    )


def test_non_databricks_cli_config_not_selected_for_pi_via_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A NON-Databricks cli-config openai default is NOT selected for pi (falls back).

    A generic (non-Databricks) cli-config provider cannot serve pi, so the pi
    fallback must skip it rather than select it (selecting it would just drop to
    Pi's own login). With no other pi-consumable default, resolution returns
    None.
    """
    _write_codex_config(
        tmp_path,
        """
model_provider = "Databricks"

[model_providers.Databricks]
name = "Some Other Proxy"
base_url = "https://proxy.example.com/v1"

[model_providers.Databricks.auth]
command = "printf"
args = ["%s", "sk-static"]
""",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    # codex-databricks here points at a non-Databricks proxy → not pi-consumable.
    assert creds.resolve_pi_native_provider(config_loader=_cli_config_databricks_config) is None


@pytest.mark.parametrize(
    "gateway_url",
    [
        # Canonical AWS gateway.
        "https://1965859176160743.ai-gateway.cloud.databricks.com/codex/v1",
        # Staging variant (still ends in .cloud.databricks.com).
        "https://wkspc.ai-gateway.staging.cloud.databricks.com/codex/v1",
        # Azure / GCP parent domains carrying the ai-gateway label.
        "https://wkspc.ai-gateway.azuredatabricks.net/codex/v1",
        "https://wkspc.ai-gateway.gcp.databricks.com/codex/v1",
    ],
)
def test_is_databricks_ai_gateway_url_accepts_real_hosts(gateway_url: str) -> None:
    """The hardened detector accepts genuine Databricks AI Gateway hosts."""
    assert creds._is_databricks_ai_gateway_url(gateway_url) is True


@pytest.mark.parametrize(
    "gateway_url",
    [
        *_LOOKALIKE_GATEWAY_URLS,
        # ai-gateway label, databricks substring, but non-databricks suffix.
        "https://ai-gateway.databricks.evil.test/codex/v1",
        # Trusted suffix but no ai-gateway label (a non-gateway Databricks host).
        "https://wkspc.cloud.databricks.com/codex/v1",
        # ai-gateway only as a substring of a label, not a full label.
        "https://my-ai-gateway-proxy.cloud.databricks.com/codex/v1",
        # Garbage / no hostname.
        "not-a-url",
        "",
    ],
)
def test_is_databricks_ai_gateway_url_rejects_lookalikes(gateway_url: str) -> None:
    """The hardened detector rejects look-alike and malformed URLs."""
    assert creds._is_databricks_ai_gateway_url(gateway_url) is False


def test_anthropic_family_ignores_wire_api() -> None:
    """The Anthropic family always uses anthropic-messages, ignoring wire_api.

    The wire_api setting is only meaningful for the OpenAI family.
    """
    config = {
        "providers": {
            "anthropic": {
                "kind": "key",
                "default": True,
                "anthropic": {
                    "base_url": "https://api.anthropic.com",
                    "api_key": "sk-test",
                    "wire_api": "chat",  # Should be ignored for Anthropic
                    "models": {"default": "claude-4"},
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(config_loader=lambda: config)
    assert provider is not None
    # Anthropic should always use anthropic-messages, not affected by wire_api
    assert provider.api == "anthropic-messages"
    assert provider.base_url == "https://api.anthropic.com"
    assert provider.model == "claude-4"
    assert provider.api_key == "sk-test"


def test_model_override_beats_databricks_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A session model override wins over the Databricks gateway default.

    This is the spec-driven model-override path: the runner reads the agent
    spec's ``executor.model`` and threads it into ``resolve_pi_native_provider``,
    so the rendered ``models.json`` selects the requested model rather than the
    ``databricks-claude-sonnet-4-6`` default.
    """
    from omnigent.inner import databricks_executor

    monkeypatch.setattr(
        databricks_executor,
        "_read_databrickscfg_host",
        lambda profile: "https://wkspc.example.com/",
    )

    provider = creds.resolve_pi_native_provider(
        model="databricks-claude-opus-4-7", config_loader=_databricks_config
    )

    assert provider is not None
    assert provider.model == "databricks-claude-opus-4-7"
    # The override flows all the way into the rendered models.json.
    cfg = provider.to_models_config()
    assert cfg["providers"]["omnigent"]["models"] == [{"id": "databricks-claude-opus-4-7"}]


def test_model_override_beats_inline_family_default() -> None:
    """A session model override wins over an inline family's default model."""
    config = {
        "providers": {
            "anthropic": {
                "kind": "key",
                "default": True,
                "anthropic": {
                    "base_url": "https://api.anthropic.com",
                    "api_key": "sk-test",
                    "models": {"default": "claude-sonnet-4-6"},
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(
        model="claude-opus-4-7", config_loader=lambda: config
    )
    assert provider is not None
    assert provider.model == "claude-opus-4-7"
    cfg = provider.to_models_config()
    assert cfg["providers"]["omnigent"]["models"] == [{"id": "claude-opus-4-7"}]


def test_databricks_prefixed_override_normalized_for_inline_anthropic() -> None:
    """A ``databricks-`` override against an inline Anthropic key provider strips.

    The spec's ``executor.model`` may be a Databricks-gateway id
    (``databricks-claude-opus-4-7``). That prefix only routes through the
    Databricks AI Gateway; an inline vendor-direct provider (here a
    key-kind ``api.anthropic.com``) cannot route it. The resolver must
    mechanically strip the prefix so the rendered ``models.json`` selects the
    bare ``claude-opus-4-7`` id the endpoint understands.
    """
    config = {
        "providers": {
            "anthropic": {
                "kind": "key",
                "default": True,
                "anthropic": {
                    "base_url": "https://api.anthropic.com",
                    "api_key": "sk-test",
                    "models": {"default": "claude-sonnet-4-6"},
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(
        model="databricks-claude-opus-4-7", config_loader=lambda: config
    )
    assert provider is not None
    # The gateway prefix is stripped for the vendor-direct Anthropic endpoint.
    assert provider.model == "claude-opus-4-7"
    cfg = provider.to_models_config()
    assert cfg["providers"]["omnigent"]["models"] == [{"id": "claude-opus-4-7"}]


def test_databricks_prefixed_override_normalized_for_inline_openai() -> None:
    """A ``databricks-`` override against an inline OpenAI provider strips too.

    Same contract as the Anthropic case for the OpenAI family: a
    ``databricks-gpt-*`` id is a gateway spelling the vendor-direct OpenAI
    endpoint cannot route, so the prefix is stripped to the bare ``gpt-*`` id.
    """
    config = {
        "providers": {
            "openai-gateway": {
                "kind": "gateway",
                "default": True,
                "openai": {
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "sk-test",
                    "models": {"default": "gpt-4o"},
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(
        model="databricks-gpt-5-4", config_loader=lambda: config
    )
    assert provider is not None
    assert provider.api == "openai-responses"
    # The gateway prefix is stripped for the vendor-direct OpenAI endpoint.
    assert provider.model == "gpt-5-4"
    cfg = provider.to_models_config()
    assert cfg["providers"]["omnigent"]["models"] == [{"id": "gpt-5-4"}]


def test_inline_family_passes_non_mechanical_override_through() -> None:
    """A non-mechanical override (slash-shaped) passes through unchanged.

    ``normalize_model_for_provider`` only strips mechanical
    ``databricks-claude-*``/``databricks-gpt-*`` ids; a custom inline-gateway
    id like ``zai-org/GLM-4.7`` has no gateway counterpart and must survive
    verbatim so the inline endpoint can route it.
    """
    config = {
        "providers": {
            "deepinfra": {
                "kind": "gateway",
                "default": True,
                "openai": {
                    "base_url": "https://api.deepinfra.com/v1/openai",
                    "api_key": "sk-test",
                    "wire_api": "chat",
                    "models": {"default": "zai-org/GLM-4.7"},
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(
        model="zai-org/GLM-4.7", config_loader=lambda: config
    )
    assert provider is not None
    assert provider.model == "zai-org/GLM-4.7"
    cfg = provider.to_models_config()
    assert cfg["providers"]["omnigent"]["models"] == [{"id": "zai-org/GLM-4.7"}]
