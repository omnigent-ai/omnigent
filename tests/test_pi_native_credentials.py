"""Tests for omnigent.pi_native_credentials (native Pi provider wiring)."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent import pi_native_credentials as creds


@pytest.fixture(autouse=True)
def _stub_catalog_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnigent.model_catalog.resolve_catalog_model",
        lambda provider_name, *, family, **kwargs: SimpleNamespace(
            model_id=f"catalog-{provider_name}-{family}-default"
        ),
    )


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
    assert provider.model == "catalog-databricks-claude-default"
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


def test_databricks_unresolvable_credentials_sets_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expired token → provider still resolves but carries a re-auth warning.

    Pi launches fine (its ``!command`` apiKey may recover), but a silent dead
    session is worse than a visible notice — so the resolver flags it.
    """
    from omnigent.inner import databricks_executor

    monkeypatch.setattr(
        databricks_executor,
        "_read_databrickscfg_host",
        lambda profile: "https://wkspc.example.com/",
    )

    def _boom(profile: str | None):
        raise OSError("refresh token is invalid")

    monkeypatch.setattr(creds, "resolve_databricks_workspace", _boom)

    provider = creds.resolve_pi_native_provider(config_loader=_databricks_config)

    assert provider is not None
    assert provider.credential_warning is not None
    assert "demo-staging" in provider.credential_warning
    assert "databricks auth login" in provider.credential_warning


def test_databricks_model_list_failure_has_no_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Creds resolve but the model-list fetch fails → benign, no warning."""
    from omnigent.inner import databricks_executor
    from omnigent.runtime.credentials import databricks as rt_databricks

    monkeypatch.setattr(
        databricks_executor,
        "_read_databrickscfg_host",
        lambda profile: "https://wkspc.example.com/",
    )
    monkeypatch.setattr(
        creds,
        "resolve_databricks_workspace",
        lambda profile: rt_databricks.WorkspaceCreds(
            host="https://wkspc.example.com", token="tok"
        ),
    )

    def _fetch_boom(host: str, token: str):
        raise RuntimeError("network blip")

    monkeypatch.setattr(creds, "_fetch_pi_model_lists", _fetch_boom)

    provider = creds.resolve_pi_native_provider(config_loader=_databricks_config)

    assert provider is not None
    assert provider.credential_warning is None


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


def test_managed_picker_prefix_is_not_part_of_provider_model() -> None:
    """A managed picker value resolves its provider-local model id."""
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
        model="omnigent/claude-opus-4-7", config_loader=lambda: config
    )

    assert provider is not None
    assert provider.model == "claude-opus-4-7"


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


def test_pi_native_provider_launch_namespaced_model_uses_qualified_arg(
    tmp_path: Path,
) -> None:
    """A model id containing '/' is passed as 'provider/model' to avoid mis-routing.

    Pi's arg parser treats 'provider/model' in --model as a provider override.
    When the model id itself contains a slash (e.g. an OpenRouter-namespaced
    id like 'moonshotai/kimi-k2.5'), passing it bare as --model causes Pi to
    route to the builtin 'moonshotai' provider (which has no API key) rather
    than our custom 'omnigent' provider. The fix qualifies the arg as
    'omnigent/moonshotai/kimi-k2.5' so Pi's findExactModelReferenceMatch
    finds the canonical form under our provider.
    """
    provider = creds.PiProviderConfig(
        provider_id="omnigent",
        base_url="https://openrouter.ai/api/v1",
        api="openai-completions",
        model="moonshotai/kimi-k2.5",
        api_key="sk-or-secret",
        auth_header=False,
    )
    agent_dir = tmp_path / "pi-agent"
    _env, args = creds.pi_native_provider_launch(agent_dir, provider)

    assert args == ["--provider", "omnigent", "--model", "omnigent/moonshotai/kimi-k2.5"]


def test_provider_launch_accepts_provider_qualified_selection(tmp_path: Path) -> None:
    """A start-picker selection chooses its generated Pi provider and model."""
    provider = creds.PiProviderConfig(
        provider_id="omnigent",
        base_url="https://api.anthropic.com",
        api="anthropic-messages",
        model="claude-sonnet-4-6",
        api_key="sk-secret",
        auth_header=False,
        additional_providers={
            "omnigent-openai": {
                "baseUrl": "https://api.openai.com/v1",
                "api": "openai-responses",
                "apiKey": "sk-openai",
                "models": [{"id": "gpt-5.6-sol"}],
            }
        },
    )

    _, args = creds.pi_native_provider_launch(
        tmp_path / "pi-agent",
        provider,
        selection="omnigent-openai/gpt-5.6-sol",
    )

    assert args == [
        "--provider",
        "omnigent-openai",
        "--model",
        "gpt-5.6-sol",
        "--thinking",
        "off",
    ]


def test_provider_launch_rejects_unavailable_qualified_selection(tmp_path: Path) -> None:
    """A stale picker value must not silently launch the provider default."""
    provider = creds.PiProviderConfig(
        provider_id="omnigent",
        base_url="https://api.anthropic.com",
        api="anthropic-messages",
        model="claude-sonnet-4-6",
        api_key="sk-secret",
        auth_header=False,
    )
    agent_dir = tmp_path / "pi-agent"

    with pytest.raises(ValueError, match="not available"):
        creds.pi_native_provider_launch(
            agent_dir,
            provider,
            selection="omnigent-openai/gpt-missing",
        )

    assert not agent_dir.exists()


def test_pi_native_model_options_lists_only_managed_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-launch choices come only from the provider built by ``omni setup``."""
    provider = creds.PiProviderConfig(
        provider_id="omnigent",
        base_url="https://api.anthropic.com",
        api="anthropic-messages",
        model="claude-sonnet-4-6",
        api_key="sk-secret",
        auth_header=False,
        additional_providers={
            "omnigent-openai": {
                "baseUrl": "https://api.openai.com/v1",
                "api": "openai-responses",
                "apiKey": "sk-openai",
                "models": [{"id": "gpt-5.6-sol", "name": "GPT 5.6 Sol"}],
            }
        },
    )
    monkeypatch.setattr(creds, "resolve_pi_native_provider", lambda: provider)

    assert creds.pi_native_model_options() == [
        {
            "id": "omnigent-openai/gpt-5.6-sol",
            "model": "omnigent-openai/gpt-5.6-sol",
            "displayName": "GPT 5.6 Sol",
        },
        {
            "id": "omnigent/claude-sonnet-4-6",
            "model": "omnigent/claude-sonnet-4-6",
            "displayName": "claude-sonnet-4-6",
        },
    ]


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
    assert provider.model == "catalog-databricks-claude-default"
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


def test_workspace_url_for_dedicated_gateway_uses_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dedicated AI Gateway origin is not itself a workspace API host."""
    from omnigent.runtime.credentials import databricks as db_creds_mod

    def resolve(profile: str | None) -> db_creds_mod.WorkspaceCreds:
        assert profile == "prod"
        return db_creds_mod.WorkspaceCreds(
            host="https://workspace.cloud.databricks.com",
            token="unused",
        )

    monkeypatch.setattr(creds, "resolve_databricks_workspace", resolve)

    assert (
        creds._databricks_workspace_url_for_gateway(
            "https://123.ai-gateway.cloud.databricks.com/anthropic",
            profile="prod",
        )
        == "https://workspace.cloud.databricks.com"
    )


def test_workspace_url_for_generic_provider_is_none() -> None:
    """Generic compatible providers are not probed through Databricks APIs."""
    assert creds._databricks_workspace_url_for_gateway("https://api.anthropic.com/v1") is None


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
    # The override flows into the rendered models.json. When the live model
    # fetch fails (no real credentials in tests), only the selected model is
    # shown — no stale hardcoded list.
    cfg = provider.to_models_config()
    model_ids = [m["id"] for m in cfg["providers"]["omnigent"]["models"]]
    assert "databricks-claude-opus-4-7" in model_ids


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


def test_databricks_profile_registers_gpt_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Databricks profile provider includes an OpenAI Completions provider for GPT models.

    The ``omnigent-openai`` provider targets ``/serving-endpoints`` so Pi's
    /model command exposes GPT models returned by the live serving-endpoints API.
    """
    from omnigent.inner import databricks_executor

    monkeypatch.setattr(
        databricks_executor,
        "_read_databrickscfg_host",
        lambda profile: "https://wkspc.example.com/",
    )
    # Mock credential resolution and live fetch — no real Databricks profile needed.
    from omnigent.runtime.credentials import databricks as db_creds_mod

    monkeypatch.setattr(
        creds,
        "resolve_databricks_workspace",
        lambda profile: db_creds_mod.WorkspaceCreds(host="https://wkspc.example.com", token="tok"),
    )
    # gpt-5-5 needs the Responses API; gpt-5-4 uses Completions
    live_gpt_responses = [{"id": "databricks-gpt-5-5", "input": ["text", "image"]}]
    live_gpt_completions = [{"id": "databricks-gpt-5-4", "input": ["text", "image"]}]
    live_claude = [{"id": "databricks-claude-sonnet-4-6", "input": ["text", "image"]}]
    monkeypatch.setattr(
        creds,
        "_fetch_pi_model_lists",
        lambda *_: (live_claude, live_gpt_responses, live_gpt_completions, []),
    )

    provider = creds.resolve_pi_native_provider(config_loader=_databricks_config)
    assert provider is not None

    cfg = provider.to_models_config()
    openai_entry = cfg["providers"].get("omnigent-openai")
    assert openai_entry is not None, (
        "omnigent-openai (responses) provider missing from models.json"
    )
    assert openai_entry["baseUrl"] == "https://wkspc.example.com/ai-gateway/codex/v1"
    assert openai_entry["api"] == "openai-responses"
    assert any(m["id"] == "databricks-gpt-5-5" for m in openai_entry["models"])
    completions_entry = cfg["providers"].get("omnigent-completions")
    assert completions_entry is not None, "omnigent-completions provider missing from models.json"
    assert completions_entry["api"] == "openai-completions"
    assert any(m["id"] == "databricks-gpt-5-4" for m in completions_entry["models"])


def test_cli_config_databricks_registers_gpt_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A cli-config provider fetches the model list via the real workspace URL.

    The AI gateway hostname is NOT the workspace hostname (stripping
    ``ai-gateway.`` produces NXDOMAIN). The fix resolves workspace credentials
    from ~/.databrickscfg (DEFAULT profile) and calls /api/2.0/serving-endpoints
    against the real workspace, so GPT and other non-Claude models appear in
    Pi's /model output.
    """
    _write_codex_config(tmp_path, _DATABRICKS_CODEX_CONFIG)
    monkeypatch.setenv("HOME", str(tmp_path))
    # Workspace URL comes from resolve_databricks_workspace (DEFAULT profile),
    # but the token for the API call comes from the auth_command — the SDK's
    # minted token may not have serving-endpoints access.
    from omnigent.runtime.credentials import databricks as db_creds_mod

    monkeypatch.setattr(
        creds,
        "resolve_databricks_workspace",
        lambda profile: db_creds_mod.WorkspaceCreds(
            host="https://dbc-a5d4177a-49dc.cloud.databricks.com", token="sdk-tok"
        ),
    )
    monkeypatch.setattr(creds, "_run_auth_command", lambda *_: "cmd-tok")
    live_gpt = [{"id": "databricks-gpt-5-4", "input": ["text", "image"]}]
    live_claude = [{"id": "databricks-claude-sonnet-4-6", "input": ["text", "image"]}]

    def _mock_fetch(workspace_url: str, token: str):
        # Assert the auth_command token is used, not the SDK token
        assert token == "cmd-tok", f"expected auth_command token, got {token!r}"
        assert "dbc-a5d4177a" in workspace_url
        return live_claude, live_gpt, [], []

    monkeypatch.setattr(creds, "_fetch_pi_model_lists", _mock_fetch)

    provider = creds.resolve_pi_native_provider(config_loader=_cli_config_databricks_config)
    assert provider is not None

    cfg = provider.to_models_config()
    openai_entry = cfg["providers"].get("omnigent-openai")
    assert openai_entry is not None, "omnigent-openai provider missing from models.json"
    # Uses the AI Gateway codex URL (supports tools); the REAL workspace hostname
    # from databrickscfg fixes the NXDOMAIN issue for dedicated-subdomain gateways.
    assert (
        openai_entry["baseUrl"]
        == "https://1965859176160743.ai-gateway.cloud.databricks.com/codex/v1"
    )
    assert openai_entry["api"] == "openai-responses"
    assert any(m["id"] == "databricks-gpt-5-4" for m in openai_entry["models"])


def test_fetch_pi_model_lists_parses_serving_endpoints() -> None:
    """_fetch_pi_model_lists uses Unity Catalog model-services API for model ids."""
    import json
    import unittest.mock

    import httpx

    def _make_service(name: str, api_types: list[str]) -> dict:
        return {
            "name": f"model-services/{name}",
            "supported_api_types": api_types,
        }

    payload = {
        "model_services": [
            _make_service("system.ai.claude-sonnet-4-6", ["mlflow/v1/chat/completions"]),
            _make_service("system.ai.claude-opus-4-8", ["mlflow/v1/chat/completions"]),
            # GPT with Responses API support
            _make_service(
                "system.ai.gpt-5-5", ["mlflow/v1/chat/completions", "openai/v1/responses"]
            ),
            # GPT completions only (older)
            _make_service(
                "system.ai.gpt-5-4", ["mlflow/v1/chat/completions", "openai/v1/responses"]
            ),
            # Future GPT metadata, deliberately Chat-only.
            _make_service("system.ai.gpt-chat-only", ["mlflow/v1/chat/completions"]),
            # Llama - chat only
            _make_service("system.ai.llama-4-maverick", ["mlflow/v1/chat/completions"]),
            # Kimi - chat only (no Responses API per UC metadata)
            _make_service("system.ai.kimi-k2-7-code", ["mlflow/v1/chat/completions"]),
            # Embedding model - should be excluded
            _make_service("system.ai.qwen3-embedding", ["mlflow/v1/embeddings"]),
        ]
    }

    class _MockTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            assert "/api/2.1/unity-catalog/model-services" in str(request.url)
            assert request.headers["authorization"].startswith("Bearer ")
            return httpx.Response(200, content=json.dumps(payload).encode())

    _real_client = httpx.Client
    with unittest.mock.patch(
        "httpx.Client",
        lambda **kw: _real_client(transport=_MockTransport()),
    ):
        claude, gpt, completions, _gemini = creds._fetch_pi_model_lists(
            "https://wkspc.example.com", "tok"
        )

    # Claude models
    claude_ids = [m["id"] for m in claude]
    assert "system.ai.claude-sonnet-4-6" in claude_ids
    assert "system.ai.claude-opus-4-8" in claude_ids
    # GPT with openai/v1/responses → gpt_responses
    gpt_ids = [m["id"] for m in gpt]
    assert "system.ai.gpt-5-5" in gpt_ids
    assert "system.ai.gpt-5-4" in gpt_ids
    assert "system.ai.kimi-k2-7-code" in gpt_ids
    # Kimi uses Responses API — no reasoning:true needed (that's completions-path only).
    kimi_entry = next(m for m in gpt if m["id"] == "system.ai.kimi-k2-7-code")
    assert kimi_entry.get("reasoning") is None
    # Llama routes to mlflow gateway (system.ai.* ids 404 at serving-endpoints).
    mlflow_ids = [m["id"] for m in _gemini]
    assert "system.ai.llama-4-maverick" in mlflow_ids
    assert "system.ai.gpt-chat-only" in mlflow_ids
    completions_ids = [m["id"] for m in completions]
    assert not completions_ids  # no completions-only models in this test payload
    # Embedding excluded
    assert "system.ai.qwen3-embedding" not in gpt_ids + completions_ids + claude_ids
    assert all(m.get("input") == ["text", "image"] for m in claude + gpt + completions)


def test_fetch_pi_model_lists_falls_back_on_http_error() -> None:
    """_fetch_pi_model_lists returns empty lists when the API call fails.

    Empty lists → to_models_config() falls back to single-model display.
    No stale hardcoded list is used.
    """
    import unittest.mock

    import httpx

    class _ErrorTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(401)

    _real_client = httpx.Client
    with unittest.mock.patch(
        "httpx.Client",
        lambda **kw: _real_client(transport=_ErrorTransport()),
    ):
        claude, gpt, completions, gemini = creds._fetch_pi_model_lists(
            "https://wkspc.example.com", "bad-tok"
        )

    assert claude == []
    assert gpt == []
    assert completions == []
    assert gemini == []


def test_fetch_pi_model_lists_carries_catalog_token_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The interactive path advertises the catalog's context and output limits.

    Pi defaults an entry with no ``contextWindow``/``maxTokens`` to 128000 /
    16384, so a 1M-context gateway model registered bare is silently capped —
    the harness path already carries the real limits, and both surfaces must
    agree. Regression guard for the two fields going missing again.
    """
    import json
    import unittest.mock

    import httpx

    from omnigent import model_catalog
    from omnigent.model_metadata import ModelMetadata

    payload = {
        "model_services": [
            {
                "name": "model-services/system.ai.claude-opus-4-8",
                "supported_api_types": ["mlflow/v1/chat/completions"],
            }
        ]
    }

    def _catalog(provider_name: str) -> tuple[model_catalog.ModelEntry, ...]:
        """Stand in for the MLflow catalog, which reports the limits.

        :param provider_name: The catalog provider being queried.
        :returns: One entry, keyed by the ``databricks-`` alias spelling so the
            alias match is exercised too.
        """
        assert provider_name == "databricks"
        return (
            model_catalog.ModelEntry(
                id="databricks-claude-opus-4-8",
                family="claude",
                metadata=ModelMetadata(context_window=1000000, max_output_tokens=128000),
            ),
        )

    monkeypatch.setattr(model_catalog, "catalog_model_entries", _catalog)

    class _MockTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            """Serve the model-services page.

            :param request: The outgoing request.
            :returns: The canned response.
            """
            return httpx.Response(200, content=json.dumps(payload).encode())

    _real_client = httpx.Client
    with unittest.mock.patch(
        "httpx.Client",
        lambda **kw: _real_client(transport=_MockTransport()),
    ):
        claude, _gpt, _completions, _gemini = creds._fetch_pi_model_lists(
            "https://wkspc.example.com", "tok"
        )

    assert [m["id"] for m in claude] == ["system.ai.claude-opus-4-8"]
    assert claude[0]["contextWindow"] == 1000000
    assert claude[0]["maxTokens"] == 128000


def test_fetch_pi_model_lists_survives_catalog_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    """An MLflow catalog outage drops the limits, never the models.

    Live availability is authoritative: losing the metadata lookup must leave
    the picker fully populated (Pi then applies its own defaults) rather than
    failing the launch.
    """
    import json
    import unittest.mock

    import httpx

    from omnigent import model_catalog

    payload = {
        "model_services": [
            {
                "name": "model-services/system.ai.claude-opus-4-8",
                "supported_api_types": ["mlflow/v1/chat/completions"],
            }
        ]
    }

    def _boom(provider_name: str) -> tuple[model_catalog.ModelEntry, ...]:
        """Fail the metadata lookup.

        :param provider_name: The catalog provider being queried.
        :returns: Never returns.
        """
        raise RuntimeError("catalog unavailable")

    monkeypatch.setattr(model_catalog, "catalog_model_entries", _boom)

    class _MockTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            """Serve the model-services page.

            :param request: The outgoing request.
            :returns: The canned response.
            """
            return httpx.Response(200, content=json.dumps(payload).encode())

    _real_client = httpx.Client
    with unittest.mock.patch(
        "httpx.Client",
        lambda **kw: _real_client(transport=_MockTransport()),
    ):
        claude, _gpt, _completions, _gemini = creds._fetch_pi_model_lists(
            "https://wkspc.example.com", "tok"
        )

    assert [m["id"] for m in claude] == ["system.ai.claude-opus-4-8"]
    assert "contextWindow" not in claude[0]


def _mock_databricks_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the Databricks profile path at a fake workspace with fake creds."""
    from omnigent.inner import databricks_executor
    from omnigent.runtime.credentials import databricks as db_creds_mod

    monkeypatch.setattr(
        databricks_executor,
        "_read_databrickscfg_host",
        lambda profile: "https://wkspc.example.com/",
    )
    monkeypatch.setattr(
        creds,
        "resolve_databricks_workspace",
        lambda profile: db_creds_mod.WorkspaceCreds(host="https://wkspc.example.com", token="tok"),
    )


def _databricks_provider_without_catalog(
    monkeypatch: pytest.MonkeyPatch, model: str
) -> creds.PiProviderConfig:
    """Resolve a Databricks provider whose live model-catalog fetch failed."""
    _mock_databricks_profile(monkeypatch)

    def _boom(*_args: object) -> None:
        raise RuntimeError("network blip")

    monkeypatch.setattr(creds, "_fetch_pi_model_lists", _boom)
    provider = creds.resolve_pi_native_provider(model=model, config_loader=_databricks_config)
    assert provider is not None
    return provider


@pytest.mark.parametrize(
    ("model", "expected_provider", "expected_api"),
    [
        # Probed: the gateway serves Responses passthrough for the system.ai.*
        # id but rejects it for the databricks-* alias of the same model.
        ("databricks-glm-5-2", "omnigent-completions", "openai-completions"),
        ("system.ai.glm-5-2", "omnigent-openai", "openai-responses"),
        ("databricks-kimi-k3", "omnigent-completions", "openai-completions"),
        ("databricks-gpt-5-5", "omnigent-openai", "openai-responses"),
        ("system.ai.gemini-3-5-flash", "omnigent-mlflow", "openai-completions"),
        ("databricks-gemini-3-5-flash", "omnigent-completions", "openai-completions"),
        ("databricks-llama-4-maverick", "omnigent-completions", "openai-completions"),
        ("databricks-deepseek-v3", "omnigent-completions", "openai-completions"),
    ],
)
def test_uncataloged_non_claude_model_routed_by_family(
    monkeypatch: pytest.MonkeyPatch, model: str, expected_provider: str, expected_api: str
) -> None:
    """A non-Claude model the catalog didn't list routes by family, not Anthropic.

    The live model-services fetch is best-effort (expired token, network blip, a
    workspace listing nothing). Registering the selected model on the primary
    regardless put GLM/Gemini/Llama on ``anthropic-messages``, where the gateway
    answers "API type 'anthropic/v1/messages' is not supported" and the turn
    hangs with no reply.
    """
    provider = _databricks_provider_without_catalog(monkeypatch, model)

    cfg = provider.to_models_config()
    assert [m["id"] for m in cfg["providers"][expected_provider]["models"]] == [model]
    assert cfg["providers"][expected_provider]["api"] == expected_api
    # The Claude-only primary must not also offer it.
    assert cfg["providers"]["omnigent"]["models"] == []
    assert provider.unroutable_model_warning() is None


def test_uncataloged_deepseek_declares_reasoning(monkeypatch: pytest.MonkeyPatch) -> None:
    """DeepSeek streams on reasoning_content; Pi needs ``reasoning: true``.

    Without the flag Pi sees empty content and the turn never completes.
    """
    provider = _databricks_provider_without_catalog(monkeypatch, "databricks-deepseek-v3")

    entry = provider.to_models_config()["providers"]["omnigent-completions"]["models"][0]
    assert entry.get("reasoning") is True


def test_uncataloged_claude_model_stays_on_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Claude model still self-registers: the primary surface serves it."""
    provider = _databricks_provider_without_catalog(monkeypatch, "databricks-claude-sonnet-4-6")

    cfg = provider.to_models_config()
    assert [m["id"] for m in cfg["providers"]["omnigent"]["models"]] == [
        "databricks-claude-sonnet-4-6"
    ]
    assert provider.unroutable_model_warning() is None


def test_uncataloged_custom_claude_endpoint_stays_on_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provisioned-throughput Claude endpoint keeps working.

    Custom endpoint names aren't enumerated by the model-services API, so the
    family fallback is all that places them — classifying on "claude" keeps a
    ``prod-claude-sonnet-pt`` endpoint on the Anthropic surface that serves it.
    """
    provider = _databricks_provider_without_catalog(monkeypatch, "prod-claude-sonnet-pt")

    cfg = provider.to_models_config()
    assert [m["id"] for m in cfg["providers"]["omnigent"]["models"]] == ["prod-claude-sonnet-pt"]
    assert provider.unroutable_model_warning() is None


def test_unparseable_model_is_refused_with_user_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model Pi cannot parse is left unregistered and the user is told why.

    Gemini 2.5 returns typed-array content Pi renders as ``[object Object]`` on
    every available wire, so no surface can serve it. Registering it anywhere
    would hang; the warning turns that into an explanation.
    """
    provider = _databricks_provider_without_catalog(monkeypatch, "databricks-gemini-2-5-pro")

    cfg = provider.to_models_config()
    assert cfg["providers"]["omnigent"]["models"] == []
    assert list(cfg["providers"]) == ["omnigent"]
    warning = provider.unroutable_model_warning()
    assert warning is not None
    assert "databricks-gemini-2-5-pro" in warning


def test_unreachable_surface_is_refused_with_user_warning() -> None:
    """A model whose surface this credential can't reach is refused, not guessed.

    The cli-config path can resolve the gateway's Responses surface but not the
    workspace ``/serving-endpoints`` URL. A completions-only model then has
    nowhere to go, so it must not fall back to the Anthropic primary.
    """
    provider = creds.PiProviderConfig(
        provider_id="omnigent",
        base_url="https://wkspc.example.com/ai-gateway/anthropic",
        api="anthropic-messages",
        model="databricks-llama-4-maverick",
        api_key="!auth",
        auth_header=True,
        databricks_surfaces={
            creds.DatabricksPiSurface.RESPONSES: "https://wkspc.example.com/ai-gateway/codex/v1",
        },
    )

    cfg = provider.to_models_config()
    assert cfg["providers"]["omnigent"]["models"] == []
    assert provider.unroutable_model_warning() is not None


def test_cataloged_model_is_not_touched_by_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fallback only fires when nothing claimed the model.

    A catalog that placed the model under a secondary provider keeps working —
    the model is served there once, and the primary must not offer it as well.
    """
    _mock_databricks_profile(monkeypatch)
    live_claude = [{"id": "databricks-claude-sonnet-4-6", "input": ["text", "image"]}]
    live_completions = [{"id": "databricks-llama-4", "input": ["text", "image"]}]
    monkeypatch.setattr(
        creds, "_fetch_pi_model_lists", lambda *_: (live_claude, [], live_completions, [])
    )

    provider = creds.resolve_pi_native_provider(
        model="databricks-llama-4", config_loader=_databricks_config
    )
    assert provider is not None

    cfg = provider.to_models_config()
    assert [m["id"] for m in cfg["providers"]["omnigent-completions"]["models"]] == [
        "databricks-llama-4"
    ]
    assert [m["id"] for m in cfg["providers"]["omnigent"]["models"]] == [
        "databricks-claude-sonnet-4-6"
    ]


def test_uncataloged_model_launch_arg_matches_rendered_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--provider`` must name the provider models.json registered the model on.

    The launch resolves the provider from the rendered config, so a model placed
    by the family fallback is selectable — and gets ``--thinking off``, without
    which the gateway 400s on ``reasoning_effort``.
    """
    provider = _databricks_provider_without_catalog(monkeypatch, "databricks-glm-5-2")
    monkeypatch.setattr(
        "omnigent.inner.pi_settings.prepare_managed_pi_agent_dir",
        lambda *_args, **_kwargs: None,
    )

    _env, args = creds.pi_native_provider_launch(tmp_path / "pi-agent", provider)

    assert args == [
        "--provider",
        "omnigent-completions",
        "--model",
        "databricks-glm-5-2",
        "--thinking",
        "off",
    ]
    cfg = json.loads((tmp_path / "pi-agent" / "models.json").read_text())
    registered = cfg["providers"]["omnigent-completions"]["models"]
    assert [m["id"] for m in registered] == ["databricks-glm-5-2"]


def test_anthropic_protocol_proxy_serves_non_claude_model() -> None:
    """An Anthropic-protocol proxy may serve non-Claude ids — don't reroute it.

    ``anthropic-messages`` alone does NOT imply "Claude only": a gateway or
    LiteLLM-style proxy speaks that protocol for arbitrary models. Only the
    Databricks gateway's ``/ai-gateway/anthropic`` surface is Claude-only, and
    its builders say so by carrying ``databricks_surfaces``.
    """
    config = {
        "providers": {
            "proxy": {
                "kind": "gateway",
                "default": True,
                "anthropic": {
                    "base_url": "https://litellm.internal.example.com/anthropic",
                    "api_key": "sk-proxy",
                },
            }
        }
    }

    provider = creds.resolve_pi_native_provider(
        model="zai-org/GLM-4.7", config_loader=lambda: config
    )
    assert provider is not None
    assert provider.api == "anthropic-messages"
    assert provider.databricks_surfaces == {}

    cfg = provider.to_models_config()
    assert [m["id"] for m in cfg["providers"]["omnigent"]["models"]] == ["zai-org/GLM-4.7"]
    assert provider.unroutable_model_warning() is None


def _dual_family_config(anthropic: bool = True, openai: bool = True) -> dict[str, object]:
    """A proxy entry exposing an Anthropic surface, an OpenAI surface, or both."""
    proxy: dict[str, object] = {"kind": "gateway", "default": True}
    if anthropic:
        proxy["anthropic"] = {
            "base_url": "https://litellm.internal.example.com/anthropic",
            "api_key": "sk-a",
        }
    if openai:
        proxy["openai"] = {
            "base_url": "https://litellm.internal.example.com/v1",
            "api_key": "sk-o",
        }
    return {"providers": {"proxy": proxy}}


@pytest.mark.parametrize(
    ("model", "expected_api", "expected_base_url"),
    [
        (
            "claude-sonnet-4-6",
            "anthropic-messages",
            "https://litellm.internal.example.com/anthropic",
        ),
        ("gpt-5-5", "openai-responses", "https://litellm.internal.example.com/v1"),
        ("glm-5-2", "openai-responses", "https://litellm.internal.example.com/v1"),
        # Tokens as "other", but no gateway serves it over Anthropic Messages.
        ("gemini-3-5-flash", "openai-responses", "https://litellm.internal.example.com/v1"),
        ("llama-4-maverick", "openai-responses", "https://litellm.internal.example.com/v1"),
    ],
)
def test_dual_family_provider_matches_model_family(
    model: str, expected_api: str, expected_base_url: str
) -> None:
    """A provider offering both surfaces serves each model from its own family.

    The family loop used to return on the first configured family, so a proxy
    with both surfaces sent every model to Anthropic Messages. A non-translating
    proxy rejects that and the turn hangs with no reply.
    """
    config = _dual_family_config()

    provider = creds.resolve_pi_native_provider(model=model, config_loader=lambda: config)

    assert provider is not None
    assert provider.api == expected_api
    assert provider.base_url == expected_base_url


@pytest.mark.parametrize(
    ("model", "anthropic", "openai", "expected_api"),
    [
        ("gpt-5-5", True, False, "anthropic-messages"),
        ("claude-sonnet-4-6", False, True, "openai-responses"),
    ],
)
def test_single_family_provider_serves_any_model(
    model: str, anthropic: bool, openai: bool, expected_api: str
) -> None:
    """One configured family serves every model — it may be protocol-translating.

    Preferring the model's family must not become a requirement: a LiteLLM
    ``/anthropic`` passthrough serves GPT ids, and an OpenAI-compatible proxy
    serves Claude ids. Excluding them would strand both.
    """
    config = _dual_family_config(anthropic=anthropic, openai=openai)

    provider = creds.resolve_pi_native_provider(model=model, config_loader=lambda: config)

    assert provider is not None
    assert provider.api == expected_api


def test_databricks_builders_carry_reachable_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Databricks profile path records every surface its credential reaches."""
    _mock_databricks_profile(monkeypatch)
    monkeypatch.setattr(creds, "_fetch_pi_model_lists", lambda *_: ([], [], [], []))

    provider = creds.resolve_pi_native_provider(config_loader=_databricks_config)

    assert provider is not None
    assert provider.databricks_surfaces == {
        creds.DatabricksPiSurface.RESPONSES: "https://wkspc.example.com/ai-gateway/codex/v1",
        creds.DatabricksPiSurface.COMPLETIONS: "https://wkspc.example.com/serving-endpoints",
        creds.DatabricksPiSurface.MLFLOW: "https://wkspc.example.com/ai-gateway/mlflow/v1",
    }


def test_launch_renders_config_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Rendering is not repeated per launch, so routing is logged once.

    ``pi_native_provider_launch`` both writes ``models.json`` and reads it back
    to resolve ``--provider``; rendering twice duplicated the routing log line.
    """
    provider = _databricks_provider_without_catalog(monkeypatch, "databricks-glm-5-2")
    monkeypatch.setattr(
        "omnigent.inner.pi_settings.prepare_managed_pi_agent_dir",
        lambda *_args, **_kwargs: None,
    )
    renders = 0
    original = creds.PiProviderConfig.to_models_config

    def _counting(self: creds.PiProviderConfig) -> object:
        nonlocal renders
        renders += 1
        return original(self)

    monkeypatch.setattr(creds.PiProviderConfig, "to_models_config", _counting)

    creds.pi_native_provider_launch(tmp_path / "pi-agent", provider)

    assert renders == 1


def test_default_claude_model_from_picks_by_tier_then_newest() -> None:
    """Pi's launch default follows the ``opus > sonnet > …`` precedence, newest first."""
    from omnigent.pi_native_credentials import _default_claude_model_from

    entries = [
        {"id": "system.ai.claude-sonnet-5"},
        {"id": "system.ai.claude-opus-4-8"},
        {"id": "system.ai.claude-opus-5"},
    ]
    # opus outranks sonnet, and opus-5 is the newest opus.
    assert _default_claude_model_from(entries) == "system.ai.claude-opus-5"
    # An empty live listing lets the caller fall through to the bundled catalog.
    assert _default_claude_model_from([]) is None
