"""Auth tests for Codex session."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.codex_native import app_server as codex_native_app_server
from omnigent.harnesses.codex_native import main as codex_native


def _write_codex_auth(path: Path, payload: object) -> None:
    """Write a test Codex auth.json payload."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _point_codex_auth_check_at(
    monkeypatch: pytest.MonkeyPatch,
    auth_path: Path,
    *,
    binary_present: bool,
    launch: Any | None = None,
    config_toml: str | None = None,
) -> None:
    """Redirect Codex availability checks away from the real machine state.

    ``launch`` pins what :func:`resolve_native_codex_launch` returns; the default
    is the defer-to-Codex-login shape (``profile=None``, ``model_provider`` not
    set → resolves to ``"openai"``), which is exactly the case where
    ``auth.json`` is the credential that decides availability. Provider-routed
    tests pass an explicit launch.

    ``config_toml`` writes Codex's config next to ``auth.json``; omitted
    means only ``auth.json`` can carry a credential.
    """
    if launch is None:
        launch = codex_native_app_server.NativeCodexLaunch(
            config_overrides=[], model=None, profile=None
        )
    # Isolate the shared codex config.toml the config-default launch reads, so a
    # defer-to-login test doesn't pick up the real machine's provider default.
    monkeypatch.setenv("CODEX_HOME", str(auth_path.parent))
    config_path = auth_path.parent / "config.toml"
    if config_toml is not None:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(config_toml, encoding="utf-8")
    monkeypatch.setattr(codex_native, "resolve_native_codex_launch", lambda model=None: launch)
    monkeypatch.setattr(
        codex_native,
        "_resolve_codex_auth_source",
        lambda: codex_native._CodexAuthSource(auth_path=auth_path, config_path=config_path),
    )
    monkeypatch.setattr(
        codex_native,
        "_find_codex_cli",
        lambda: "/tmp/codex" if binary_present else None,
    )
    if binary_present:
        # Treat a present binary as passing the version check so these
        # tests exercise only the auth-path decision.
        monkeypatch.setattr(
            "omnigent.onboarding.harness_install.harness_cli_installed",
            lambda _key, **_kw: True,
        )


def test_codex_auth_unavailable_reason_binary_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Missing codex binary reports binary-missing before reading auth.json."""
    auth_path = tmp_path / "codex-home" / "auth.json"
    _point_codex_auth_check_at(monkeypatch, auth_path, binary_present=False)

    assert codex_native._codex_auth_unavailable_reason() == "binary-missing"


def test_codex_auth_unavailable_reason_absent_auth_json_needs_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Installed codex without auth.json reports needs-auth."""
    auth_path = tmp_path / "codex-home" / "auth.json"
    _point_codex_auth_check_at(monkeypatch, auth_path, binary_present=True)

    assert codex_native._codex_auth_unavailable_reason() == "needs-auth"


def test_codex_auth_unavailable_reason_chatgpt_tokens_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real ChatGPT/OAuth auth.json (tokens block) is available.

    Mirrors the openai/codex ``AuthDotJson`` shape: ``auth_mode=chatgpt`` with a
    ``tokens`` object. There is no top-level expiry field — access-token expiry
    lives in the JWT and is refreshed via ``refresh_token`` — so presence of the
    tokens is what marks the credential configured.
    """
    auth_path = tmp_path / "codex-home" / "auth.json"
    _point_codex_auth_check_at(monkeypatch, auth_path, binary_present=True)
    _write_codex_auth(
        auth_path,
        {
            "auth_mode": "chatgpt",
            "tokens": {
                "id_token": "header.payload.sig",
                "access_token": "header.payload.sig",
                "refresh_token": "opaque-refresh",
                "account_id": "org_test",
            },
            "last_refresh": "2026-06-25T15:04:05Z",
        },
    )

    assert codex_native._codex_auth_unavailable_reason() is None


def test_codex_auth_unavailable_reason_api_key_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real API-key auth.json (``auth_mode=api``) is available."""
    auth_path = tmp_path / "codex-home" / "auth.json"
    _point_codex_auth_check_at(monkeypatch, auth_path, binary_present=True)
    _write_codex_auth(auth_path, {"auth_mode": "api", "OPENAI_API_KEY": "sk-test"})

    assert codex_native._codex_auth_unavailable_reason() is None


def test_codex_auth_unavailable_reason_no_credential_needs_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A parseable auth.json with no credential field reports needs-auth.

    e.g. a stub that records ``auth_mode`` but carries neither an
    ``OPENAI_API_KEY`` nor a ``tokens`` block — there is nothing to authenticate
    with, so the picker should warn rather than show Codex as ready.
    """
    auth_path = tmp_path / "codex-home" / "auth.json"
    _point_codex_auth_check_at(monkeypatch, auth_path, binary_present=True)
    _write_codex_auth(auth_path, {"auth_mode": "chatgpt"})

    assert codex_native._codex_auth_unavailable_reason() == "needs-auth"


def test_codex_auth_unavailable_reason_malformed_auth_needs_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Installed codex with malformed auth.json reports needs-auth."""
    auth_path = tmp_path / "codex-home" / "auth.json"
    _point_codex_auth_check_at(monkeypatch, auth_path, binary_present=True)
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    auth_path.write_text("{not json", encoding="utf-8")

    assert codex_native._codex_auth_unavailable_reason() == "needs-auth"


def test_codex_auth_unavailable_reason_databricks_profile_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A Databricks-profile launch is available even with an EMPTY auth.json.

    The reported bug: the launch mints its bearer via ``databricks auth token``
    and never reads ``auth.json``, so gating on it is a false negative. auth.json
    is deliberately absent here — availability must come from the launch.
    """
    auth_path = tmp_path / "codex-home" / "auth.json"  # never created
    launch = codex_native_app_server.NativeCodexLaunch(
        config_overrides=[], model=None, profile="my-profile"
    )
    _point_codex_auth_check_at(monkeypatch, auth_path, binary_present=True, launch=launch)

    assert codex_native._codex_auth_unavailable_reason() is None


def test_codex_auth_unavailable_reason_provider_override_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A launch pinning a non-openai model_provider is available sans auth.json."""
    auth_path = tmp_path / "codex-home" / "auth.json"  # never created
    launch = codex_native_app_server.NativeCodexLaunch(
        config_overrides=['model_provider="omnigent_databricks"'], model=None, profile=None
    )
    _point_codex_auth_check_at(monkeypatch, auth_path, binary_present=True, launch=launch)

    assert codex_native._codex_auth_unavailable_reason() is None


def test_codex_auth_unavailable_reason_config_default_provider_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty-override launch routed by the config.toml provider default is ready.

    omnigent pins no provider (empty overrides, profile None, meta "openai") but
    defers to Codex's own config.toml top-level ``model_provider`` default — a
    Databricks AIGW provider. auth.json is deliberately absent; availability must
    come from the resolvable launch base URL.
    """
    codex_home = tmp_path / "codex-home"
    auth_path = codex_home / "auth.json"  # never created
    _point_codex_auth_check_at(monkeypatch, auth_path, binary_present=True)
    from omnigent.onboarding import detected

    monkeypatch.setattr(detected, "codex_config_provider_dismissed", lambda _config: False)
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "config.toml").write_text(
        'model_provider = "Databricks"\n[model_providers.Databricks]\n'
        'base_url = "https://example.cloud.databricks.com/ai-gateway/codex/v1"\n',
        encoding="utf-8",
    )

    assert codex_native._codex_auth_unavailable_reason() is None


def test_codex_auth_unavailable_reason_explicit_openai_needs_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit ``model_provider="openai"`` launch still gates on auth.json.

    Even with a Databricks provider default in config.toml, an explicit openai
    pin is Codex's built-in login — a logged-out openai user must report
    needs-auth, never read the config default.
    """
    codex_home = tmp_path / "codex-home"
    auth_path = codex_home / "auth.json"  # never created
    launch = codex_native_app_server.NativeCodexLaunch(
        config_overrides=['model_provider="openai"'], model=None, profile=None
    )
    _point_codex_auth_check_at(monkeypatch, auth_path, binary_present=True, launch=launch)
    from omnigent.onboarding import detected

    monkeypatch.setattr(detected, "codex_config_provider_dismissed", lambda _config: False)
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "config.toml").write_text(
        'model_provider = "Databricks"\n[model_providers.Databricks]\n'
        'base_url = "https://example.cloud.databricks.com/ai-gateway/codex/v1"\n',
        encoding="utf-8",
    )

    assert codex_native._codex_auth_unavailable_reason() == "needs-auth"


#: A Codex ``config.toml`` selecting a custom gateway provider that
#: authenticates from an environment variable, the shape reported in the
#: "needs-auth with an authenticated custom provider" bug. The variable is
#: ``OPENAI_``-prefixed so the native launch's env filter passes it through.
_ENV_KEY_CONFIG_TOML = """\
model_provider = "aigateway"

[model_providers.aigateway]
name = "Unity Gateway"
base_url = "https://gateway.example.com/openai/v1"
env_key = "OPENAI_EXAMPLE_GATEWAY_TOKEN"
wire_api = "responses"
"""


def test_codex_auth_unavailable_reason_config_env_key_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A config-selected provider with a populated ``env_key`` is available.

    The reported bug: the launch leaves Codex's own provider selection alone, so
    Codex routes through the custom ``[model_providers.X]`` table and never reads
    ``auth.json`` — gating on the (legitimately absent) ``auth.json`` reported
    ``needs-auth`` and told the user to run ``codex login``, which would add a
    second credential that bypasses the gateway.
    """
    auth_path = tmp_path / "codex-home" / "auth.json"  # never created
    _point_codex_auth_check_at(
        monkeypatch, auth_path, binary_present=True, config_toml=_ENV_KEY_CONFIG_TOML
    )
    monkeypatch.setenv("OPENAI_EXAMPLE_GATEWAY_TOKEN", "gw-token")

    assert codex_native._codex_auth_unavailable_reason() is None


@pytest.mark.parametrize("value", [None, "", "   "])
def test_codex_auth_unavailable_reason_config_env_key_unset_needs_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str | None
) -> None:
    """A config-selected provider whose ``env_key`` is unset/empty needs auth.

    The credential the config names is not there, and no ``auth.json`` backs it
    up, so Codex would fail to authenticate — the warning is correct here.
    """
    auth_path = tmp_path / "codex-home" / "auth.json"  # never created
    _point_codex_auth_check_at(
        monkeypatch, auth_path, binary_present=True, config_toml=_ENV_KEY_CONFIG_TOML
    )
    if value is None:
        monkeypatch.delenv("OPENAI_EXAMPLE_GATEWAY_TOKEN", raising=False)
    else:
        monkeypatch.setenv("OPENAI_EXAMPLE_GATEWAY_TOKEN", value)

    assert codex_native._codex_auth_unavailable_reason() == "needs-auth"


def test_codex_auth_unavailable_reason_missing_provider_env_ignores_auth_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A custom provider's missing env key is not masked by a Codex login.

    Codex selects the custom provider before considering ``auth.json``. Its
    declared env credential is therefore the gate even when a valid login file
    also exists.
    """
    auth_path = tmp_path / "codex-home" / "auth.json"
    _point_codex_auth_check_at(
        monkeypatch, auth_path, binary_present=True, config_toml=_ENV_KEY_CONFIG_TOML
    )
    monkeypatch.delenv("OPENAI_EXAMPLE_GATEWAY_TOKEN", raising=False)
    _write_codex_auth(auth_path, {"OPENAI_API_KEY": "sk-unused"})

    assert codex_native._codex_auth_unavailable_reason() == "needs-auth"


def test_codex_auth_unavailable_reason_config_env_key_scrubbed_needs_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An ``env_key`` the launch env scrubs is not reported as available.

    Readiness resolves the variable in the same filtered env the launch hands
    the CLI, so a variable outside Codex's allowed env families (here a bare
    ``EXAMPLE_GATEWAY_TOKEN``) cannot mark the harness ready and then 401 on the
    first turn.
    """
    auth_path = tmp_path / "codex-home" / "auth.json"  # never created
    _point_codex_auth_check_at(
        monkeypatch,
        auth_path,
        binary_present=True,
        config_toml=_ENV_KEY_CONFIG_TOML.replace(
            'env_key = "OPENAI_EXAMPLE_GATEWAY_TOKEN"', 'env_key = "EXAMPLE_GATEWAY_TOKEN"'
        ),
    )
    monkeypatch.setenv("EXAMPLE_GATEWAY_TOKEN", "gw-token")

    assert codex_native._codex_auth_unavailable_reason() == "needs-auth"


def test_codex_auth_unavailable_reason_config_auth_command_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A config-selected provider with a token-printing auth command is available.

    Same generic rule as the ``env_key`` case, a different credential shape: the
    config names the credential, so no vendor is special-cased.
    """
    auth_path = tmp_path / "codex-home" / "auth.json"  # never created
    _point_codex_auth_check_at(
        monkeypatch,
        auth_path,
        binary_present=True,
        config_toml=(
            'model_provider = "gw"\n'
            "[model_providers.gw]\n"
            'base_url = "https://gateway.example.com/openai/v1"\n'
            "[model_providers.gw.auth]\n"
            'command = "print-token"\n'
        ),
    )

    assert codex_native._codex_auth_unavailable_reason() is None


def test_codex_auth_unavailable_reason_config_local_provider_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A local provider needs no ``auth.json`` credential."""
    auth_path = tmp_path / "codex-home" / "auth.json"  # never created
    _point_codex_auth_check_at(
        monkeypatch,
        auth_path,
        binary_present=True,
        config_toml='model_provider = "ollama"\n',
    )

    assert codex_native._codex_auth_unavailable_reason() is None


def test_codex_auth_unavailable_reason_config_builtin_provider_needs_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A config selecting the built-in provider still gates on ``auth.json``.

    ``model_provider = "openai"`` is Codex's own login, so an absent
    ``auth.json`` is a real ``needs-auth`` — the config check must not fail open
    for every config file that happens to exist.
    """
    auth_path = tmp_path / "codex-home" / "auth.json"  # never created
    _point_codex_auth_check_at(
        monkeypatch,
        auth_path,
        binary_present=True,
        config_toml='model_provider = "openai"\nmodel = "gpt-5.4"\n',
    )

    assert codex_native._codex_auth_unavailable_reason() == "needs-auth"


def test_codex_auth_unavailable_reason_dismissed_config_provider_needs_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A launch pinning the built-in provider ignores the config's credential.

    When the user removed (dismissed) the detected config provider, the launch
    pins ``model_provider="openai"``, so the config table is not what Codex
    routes through and its ``env_key`` cannot make the harness available.
    """
    auth_path = tmp_path / "codex-home" / "auth.json"  # never created
    launch = codex_native_app_server.NativeCodexLaunch(
        config_overrides=['model_provider="openai"'], model=None, profile=None
    )
    _point_codex_auth_check_at(
        monkeypatch,
        auth_path,
        binary_present=True,
        launch=launch,
        config_toml=_ENV_KEY_CONFIG_TOML,
    )
    monkeypatch.setenv("OPENAI_EXAMPLE_GATEWAY_TOKEN", "gw-token")

    assert codex_native._codex_auth_unavailable_reason() == "needs-auth"


def test_codex_auth_unavailable_reason_missing_provider_table_ignores_auth_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A broken custom-provider selection is not masked by a Codex login."""
    auth_path = tmp_path / "codex-home" / "auth.json"
    _point_codex_auth_check_at(
        monkeypatch,
        auth_path,
        binary_present=True,
        config_toml='model_provider = "missing-table"\n',
    )
    _write_codex_auth(auth_path, {"OPENAI_API_KEY": "sk-unused"})

    assert codex_native._codex_auth_unavailable_reason() == "needs-auth"


def test_codex_auth_unavailable_reason_local_auth_failure_needs_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unexpected local-auth errors never escape the readiness refresh."""
    auth_path = tmp_path / "codex-home" / "auth.json"
    _point_codex_auth_check_at(
        monkeypatch,
        auth_path,
        binary_present=True,
        config_toml=_ENV_KEY_CONFIG_TOML,
    )

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("broken local auth check")

    monkeypatch.setattr(
        "omnigent.onboarding.codex_auth_readiness.codex_config_effective_auth",
        _boom,
    )

    assert codex_native._codex_auth_unavailable_reason() == "needs-auth"


def test_codex_auth_unavailable_reason_resolver_failure_falls_back_to_auth_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A resolver blow-up fails safe onto the auth.json check (never raises)."""
    auth_path = tmp_path / "codex-home" / "auth.json"
    _point_codex_auth_check_at(monkeypatch, auth_path, binary_present=True)

    def _boom(model: object = None) -> object:
        raise RuntimeError("corrupt config")

    monkeypatch.setattr(codex_native, "resolve_native_codex_launch", _boom)

    # No auth.json → falls through to needs-auth rather than propagating.
    assert codex_native._codex_auth_unavailable_reason() == "needs-auth"


def test_codex_auth_unavailable_reason_resolver_failure_skips_config_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed launch resolution cannot assume the config provider is active."""
    auth_path = tmp_path / "codex-home" / "auth.json"
    _point_codex_auth_check_at(
        monkeypatch,
        auth_path,
        binary_present=True,
        config_toml=_ENV_KEY_CONFIG_TOML,
    )
    monkeypatch.setenv("OPENAI_EXAMPLE_GATEWAY_TOKEN", "gw-token")

    def _boom(model: object = None) -> object:
        raise RuntimeError("corrupt config")

    monkeypatch.setattr(codex_native, "resolve_native_codex_launch", _boom)

    assert codex_native._codex_auth_unavailable_reason() == "needs-auth"
