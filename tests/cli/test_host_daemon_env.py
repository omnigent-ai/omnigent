"""Host-daemon environment boundary regression tests."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final
from unittest.mock import patch

import keyring.errors
import pytest

from omnigent.cli import _build_host_daemon_env
from omnigent.host.connect import (
    RUNNER_ENV_PASSTHROUGH_ENV_VAR,
    _build_runner_env,
)
from omnigent.onboarding.provider_config import resolve_secret
from omnigent.runner.identity import (
    RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR,
    strip_runner_auth_secrets,
)

_REMOTE_SERVER_URL: Final = "https://example.databricksapps.com"
_PROXY_ENV: Final = {
    "HTTP_PROXY": "http://upper-http-proxy.example.com:3128",
    "HTTPS_PROXY": "http://upper-https-proxy.example.com:3128",
    "ALL_PROXY": "socks5://upper-proxy.example.com:1080",
    "NO_PROXY": "localhost,127.0.0.1",
    "http_proxy": "http://lower-http-proxy.example.com:3128",
    "https_proxy": "http://lower-https-proxy.example.com:3128",
    "all_proxy": "socks5://lower-proxy.example.com:1080",
    "no_proxy": "localhost,127.0.0.2",
}


@pytest.mark.parametrize("server_url", [None, _REMOTE_SERVER_URL])
@pytest.mark.parametrize("path_env", ["OMNIGENT_CODEX_PATH", "OMNIGENT_PI_PATH"])
@pytest.mark.parametrize("executable_path", [None, "/selected install/bin/harness"])
def test_executable_selection_survives_daemon_and_runner_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    server_url: str | None,
    path_env: str,
    executable_path: str | None,
) -> None:
    """Keep an explicit executable choice without widening secret forwarding."""
    monkeypatch.delenv(path_env, raising=False)
    monkeypatch.delenv(RUNNER_ENV_PASSTHROUGH_ENV_VAR, raising=False)
    if executable_path is not None:
        monkeypatch.setenv(path_env, executable_path)
    monkeypatch.setenv("OMNIGENT_UNRELATED_SECRET", "synthetic-secret")
    monkeypatch.setenv("UNRELATED_SECRET", "synthetic-secret")
    monkeypatch.setattr("omnigent.onboarding.provider_config.load_config", dict)

    daemon_env = _build_host_daemon_env(server_url=server_url)
    runner_env = _build_runner_env(
        daemon_env,
        server_url=server_url or "http://localhost:6767",
        runner_id="runner_harness_path",
        binding_token="synthetic-binding-token",
        workspace="/tmp/workspace",
        parent_pid=12345,
    )
    terminal_env = strip_runner_auth_secrets(runner_env)

    for env in (daemon_env, runner_env, terminal_env):
        assert env.get(path_env) == executable_path
        assert "UNRELATED_SECRET" not in env
    assert "OMNIGENT_UNRELATED_SECRET" not in runner_env
    if server_url:
        assert "OMNIGENT_UNRELATED_SECRET" not in daemon_env
    assert runner_env[RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR] == "synthetic-binding-token"
    assert RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR not in terminal_env


@pytest.mark.parametrize("server_url", [None, _REMOTE_SERVER_URL])
def test_pi_env_denylist_reaches_runner_through_host_daemon(
    monkeypatch: pytest.MonkeyPatch,
    server_url: str | None,
) -> None:
    """Pi's denylist crosses both hops without forwarding the named secrets."""
    denylist = "ANTHROPIC_AUTH_TOKEN, UNRELATED_SECRET"
    monkeypatch.setenv("OMNIGENT_PI_ENV_UNSET", denylist)
    monkeypatch.setenv("UNRELATED_SECRET", "synthetic-unrelated-secret")
    monkeypatch.setattr("omnigent.onboarding.provider_config.load_config", dict)

    daemon_env = _build_host_daemon_env(server_url=server_url)
    runner_env = _build_runner_env(
        daemon_env,
        server_url=server_url or "http://localhost:6767",
        runner_id="runner_pi_env",
        binding_token="binding-pi-env",
        workspace="/tmp/workspace",
        parent_pid=12345,
    )

    for env in (daemon_env, runner_env):
        assert env["OMNIGENT_PI_ENV_UNSET"] == denylist
        assert "UNRELATED_SECRET" not in env


@pytest.mark.parametrize("server_url", [None, _REMOTE_SERVER_URL])
def test_runner_can_read_keyring_from_cli_desktop_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    server_url: str | None,
) -> None:
    session_env = {
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "XDG_RUNTIME_DIR": "/run/user/1000",
    }
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("OMNIGENT_DISABLE_KEYRING", raising=False)
    for name, value in session_env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-forward")
    monkeypatch.setenv("DBUS_UNRELATED_SECRET", "must-not-forward")
    monkeypatch.setenv("XDG_UNRELATED_SECRET", "must-not-forward")

    def get_password(service: str, username: str) -> str:
        assert (service, username) == ("omnigent", "openrouter")
        if any(os.environ.get(name) != value for name, value in session_env.items()):
            raise keyring.errors.KeyringError("desktop session unavailable")
        return "test-openrouter-key"

    monkeypatch.setattr(keyring, "get_password", get_password)
    assert resolve_secret("keychain:openrouter") == "test-openrouter-key"

    daemon_env = _build_host_daemon_env(server_url=server_url)
    runner_env = _build_runner_env(
        daemon_env,
        server_url=server_url or "http://localhost:6767",
        runner_id="runner_keyring",
        binding_token="binding-keyring",
        workspace=str(tmp_path),
        parent_pid=12345,
    )

    for env in (daemon_env, runner_env):
        assert {name: env.get(name) for name in session_env} == session_env
        assert {"UNRELATED_SECRET", "DBUS_UNRELATED_SECRET", "XDG_UNRELATED_SECRET"}.isdisjoint(
            env
        )
    with patch.dict(os.environ, runner_env, clear=True):
        assert resolve_secret("keychain:openrouter") == "test-openrouter-key"


@pytest.mark.parametrize(
    ("server_url", "keeps_provider_secret"),
    [(None, True), (_REMOTE_SERVER_URL, False)],
)
def test_host_daemon_env_preserves_proxy_vars_and_provider_secret_split(
    monkeypatch: pytest.MonkeyPatch,
    server_url: str | None,
    keeps_provider_secret: bool,
) -> None:
    """Proxy selectors reach both daemon modes without widening provider secrets."""
    # Given
    for name, value in _PROXY_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "corp")
    monkeypatch.setenv("OPENAI_API_KEY", "local-provider-secret")

    # When
    env = _build_host_daemon_env(server_url=server_url)

    # Then
    assert {name: env.get(name) for name in _PROXY_ENV} == _PROXY_ENV
    assert env["DATABRICKS_CONFIG_PROFILE"] == "corp"
    assert ("OPENAI_API_KEY" in env) is keeps_provider_secret


def test_runner_env_excludes_proxy_vars_by_default() -> None:
    """Daemon proxy credentials do not cross into runner subprocesses by default."""
    # Given
    base_env = {"PATH": "/usr/bin", **_PROXY_ENV}

    # When
    env = _build_runner_env(
        base_env,
        server_url=_REMOTE_SERVER_URL,
        runner_id="runner_proxy",
        binding_token="binding-proxy",
        workspace="/tmp/workspace",
        parent_pid=12345,
    )

    # Then
    assert set(_PROXY_ENV).isdisjoint(env)


def test_runner_env_explicit_proxy_passthrough_remains_available() -> None:
    """Runner proxy forwarding remains opt-in through the existing passthrough."""
    # Given
    explicit_names = {"HTTPS_PROXY", "http_proxy"}
    base_env = {
        "PATH": "/usr/bin",
        **_PROXY_ENV,
        RUNNER_ENV_PASSTHROUGH_ENV_VAR: ",".join(explicit_names),
    }

    # When
    env = _build_runner_env(
        base_env,
        server_url=_REMOTE_SERVER_URL,
        runner_id="runner_proxy",
        binding_token="binding-proxy",
        workspace="/tmp/workspace",
        parent_pid=12345,
    )

    # Then
    assert {name: env[name] for name in explicit_names} == {
        name: _PROXY_ENV[name] for name in explicit_names
    }
    assert (set(_PROXY_ENV) - explicit_names).isdisjoint(env)


@pytest.mark.parametrize("server_url", [None, _REMOTE_SERVER_URL])
@pytest.mark.parametrize(
    ("name", "value", "expected_osc"),
    [
        ("OMNIGENT_HOST_SLICE_KEY_ENABLED", "0", True),
        ("OMNIGENT_HOST_SLICE_KEY_ENABLED", "1", True),
        ("OMNIGENT_FEATURES", None, True),
        ("OMNIGENT_FEATURES", "", True),
        ("OMNIGENT_FEATURES", "usage_page,claude_osc_status", True),
        ("OMNIGENT_FEATURES", "usage_page,-claude_osc_status", False),
        ("OMNIGENT_FEATURES", "-claude_osc_status,claude_osc_status", False),
    ],
)
def test_release_controls_reach_daemon_and_runner(
    monkeypatch: pytest.MonkeyPatch,
    server_url: str | None,
    name: str,
    value: str | None,
    expected_osc: bool,
) -> None:
    """Operator controls survive both environment boundaries in either server mode."""
    monkeypatch.delenv(name, raising=False)
    if value is not None:
        monkeypatch.setenv(name, value)

    daemon_env = _build_host_daemon_env(server_url=server_url)
    runner_env = _build_runner_env(
        daemon_env,
        server_url=_REMOTE_SERVER_URL,
        runner_id="runner_slice_key",
        binding_token="binding-slice-key",
        workspace="/tmp/workspace",
        parent_pid=12345,
    )

    assert daemon_env.get(name) == value
    assert runner_env.get(name) == value
    if name == "OMNIGENT_FEATURES":
        from omnigent.runner.resource_registry import SessionResourceRegistry
        from omnigent.server.feature_flags import Feature

        with patch.dict(os.environ, runner_env, clear=True):
            registry = SessionResourceRegistry()
        assert registry._feature_flags.enabled(Feature.CLAUDE_OSC_STATUS) == expected_osc


_CLAUDE_TOOL_SEARCH_ENV: Final = {
    "CLAUDE_CODE_USE_GATEWAY": "1",
    "ENABLE_TOOL_SEARCH": "true",
}


@pytest.mark.parametrize("server_url", [None, _REMOTE_SERVER_URL])
def test_host_daemon_env_preserves_claude_tool_search_flags(
    monkeypatch: pytest.MonkeyPatch,
    server_url: str | None,
) -> None:
    """USE_GATEWAY / ENABLE_TOOL_SEARCH reach the daemon in both modes (Gate 1)."""
    # Given
    for name, value in _CLAUDE_TOOL_SEARCH_ENV.items():
        monkeypatch.setenv(name, value)

    # When
    env = _build_host_daemon_env(server_url=server_url)

    # Then
    assert {name: env.get(name) for name in _CLAUDE_TOOL_SEARCH_ENV} == _CLAUDE_TOOL_SEARCH_ENV


def test_runner_env_preserves_claude_tool_search_flags() -> None:
    """USE_GATEWAY / ENABLE_TOOL_SEARCH reach the runner subprocess (Gate 2)."""
    # Given
    base_env = {"PATH": "/usr/bin", **_CLAUDE_TOOL_SEARCH_ENV}

    # When
    env = _build_runner_env(
        base_env,
        server_url=_REMOTE_SERVER_URL,
        runner_id="runner_tool_search",
        binding_token="binding-tool-search",
        workspace="/tmp/workspace",
        parent_pid=12345,
    )

    # Then
    assert {name: env.get(name) for name in _CLAUDE_TOOL_SEARCH_ENV} == _CLAUDE_TOOL_SEARCH_ENV


# The gcloud ADC auth selectors the Antigravity CLI (agy) reads. Non-secret
# selectors/paths (the ADC file *contains* the credential; the vars just point
# at it), so they ride the allowlists like KUBECONFIG rather than the
# credential passthrough. The CLOUDSDK_ entries are exact names on purpose:
# gcloud's CLOUDSDK_AUTH_* vars carry live tokens and must stay stripped.
_GCLOUD_ADC_ENV: Final = {
    "AGY_ADC_AUTH": "true",
    "GOOGLE_APPLICATION_CREDENTIALS": "/home/alice/.config/gcloud/adc.json",
    "GOOGLE_CLOUD_PROJECT": "acme-dev",
    "GOOGLE_CLOUD_QUOTA_PROJECT": "acme-quota",
    "CLOUDSDK_CONFIG": "/home/alice/.config/gcloud",
    "CLOUDSDK_ACTIVE_CONFIG_NAME": "alt",
}


@pytest.mark.parametrize("server_url", [None, _REMOTE_SERVER_URL])
def test_host_daemon_env_preserves_gcloud_adc_selectors(
    monkeypatch: pytest.MonkeyPatch,
    server_url: str | None,
) -> None:
    """The gcloud ADC selectors survive the CLI→daemon strip in both modes.

    Without them the detached daemon loses the user's gcloud login, so every
    antigravity-native pane it (transitively) spawns blocks at agy's
    interactive "Select login method" menu.
    """
    # Given
    for name, value in _GCLOUD_ADC_ENV.items():
        monkeypatch.setenv(name, value)

    # When
    env = _build_host_daemon_env(server_url=server_url)

    # Then
    assert {name: env.get(name) for name in _GCLOUD_ADC_ENV} == _GCLOUD_ADC_ENV


def test_runner_env_preserves_gcloud_adc_selectors() -> None:
    """The gcloud ADC selectors survive the daemon→runner strip.

    The runner env is what the antigravity-native pane ultimately inherits,
    so this hop is where a drop turns into agy's login menu.
    """
    # Given
    base_env = {"PATH": "/usr/bin", **_GCLOUD_ADC_ENV}

    # When
    env = _build_runner_env(
        base_env,
        server_url=_REMOTE_SERVER_URL,
        runner_id="runner_adc",
        binding_token="binding-adc",
        workspace="/tmp/workspace",
        parent_pid=12345,
    )

    # Then
    assert {name: env.get(name) for name in _GCLOUD_ADC_ENV} == _GCLOUD_ADC_ENV


# gcloud vars that hold live credentials, not selectors. They must NOT ride
# the allowlists: a CLOUDSDK_ prefix entry would hand usable GCP tokens to the
# runner (and transitively the agent-controlled harness).
_GCLOUD_TOKEN_ENV: Final = {
    "CLOUDSDK_AUTH_ACCESS_TOKEN": "ya29.fake-access-token",
    "CLOUDSDK_AUTH_REFRESH_TOKEN": "1//fake-refresh-token",
}


@pytest.mark.parametrize("server_url", [None, _REMOTE_SERVER_URL])
def test_host_daemon_env_strips_gcloud_auth_tokens(
    monkeypatch: pytest.MonkeyPatch,
    server_url: str | None,
) -> None:
    """gcloud CLOUDSDK_AUTH_* bearer/refresh tokens do NOT reach the daemon."""
    # Given
    for name, value in _GCLOUD_TOKEN_ENV.items():
        monkeypatch.setenv(name, value)

    # When
    env = _build_host_daemon_env(server_url=server_url)

    # Then
    assert not set(_GCLOUD_TOKEN_ENV) & set(env)


def test_runner_env_strips_gcloud_auth_tokens() -> None:
    """gcloud CLOUDSDK_AUTH_* bearer/refresh tokens do NOT reach the runner."""
    # Given
    base_env = {"PATH": "/usr/bin", **_GCLOUD_TOKEN_ENV}

    # When
    env = _build_runner_env(
        base_env,
        server_url=_REMOTE_SERVER_URL,
        runner_id="runner_adc_tokens",
        binding_token="binding-adc-tokens",
        workspace="/tmp/workspace",
        parent_pid=12345,
    )

    # Then
    assert not set(_GCLOUD_TOKEN_ENV) & set(env)


@pytest.mark.parametrize("server_url", [None, _REMOTE_SERVER_URL])
def test_host_daemon_env_preserves_claude_telemetry_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    server_url: str | None,
) -> None:
    """CLAUDE_CODE_ENABLE_TELEMETRY survives the CLI→daemon strip in both modes."""
    # Given
    monkeypatch.setenv("CLAUDE_CODE_ENABLE_TELEMETRY", "1")
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "otlp")

    # When
    env = _build_host_daemon_env(server_url=server_url)

    # Then: the opt-in flag travels with the OTEL exporter config it belongs to.
    assert env.get("OTEL_METRICS_EXPORTER") == "otlp"
    assert env.get("CLAUDE_CODE_ENABLE_TELEMETRY") == "1"


def test_runner_env_preserves_claude_telemetry_opt_in() -> None:
    """CLAUDE_CODE_ENABLE_TELEMETRY survives the daemon→runner strip."""
    # Given
    base_env = {
        "PATH": "/usr/bin",
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
    }

    # When
    env = _build_runner_env(
        base_env,
        server_url=_REMOTE_SERVER_URL,
        runner_id="runner_telemetry",
        binding_token="binding-telemetry",
        workspace="/tmp/workspace",
        parent_pid=12345,
    )

    # Then
    assert env.get("OTEL_METRICS_EXPORTER") == "otlp"
    assert env.get("CLAUDE_CODE_ENABLE_TELEMETRY") == "1"


_TELEMETRY_OPTOUTS = [
    ("CI", "true"),
    ("CI", ""),
    ("GITHUB_ACTIONS", "true"),
    ("PYTEST_CURRENT_TEST", "tests/example.py::test_example (call)"),
    ("CIRCLECI", "true"),
    ("JENKINS_URL", "https://ci.example.com/private-job"),
    ("TRAVIS", "true"),
    ("GITLAB_CI", "true"),
    ("TF_BUILD", "true"),
    ("BITBUCKET_BUILD_NUMBER", "123"),
    ("CODEBUILD_BUILD_ARN", "private-build-identifier"),
    ("BUILDKITE", "true"),
    ("TEAMCITY_VERSION", "2026.1"),
    ("DO_NOT_TRACK", "1"),
    ("DISABLE_TELEMETRY", "true"),
    ("OMNIGENT_ANALYTICS", "0"),
    ("OMNIGENT_DISABLE_TELEMETRY", "yes"),
]


@pytest.mark.parametrize(("name", "value"), _TELEMETRY_OPTOUTS)
@pytest.mark.parametrize("via_daemon", [True, False], ids=["daemon", "direct-server"])
def test_local_server_preserves_usage_telemetry_opt_out(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    value: str,
    via_daemon: bool,
) -> None:
    """Daemon-backed and direct server launches preserve startup suppression."""
    import omnigent.telemetry.client as telemetry
    from omnigent.host import local_server

    source = {
        "PATH": os.defpath,
        "OMNIGENT_DATA_DIR": str(tmp_path / "data"),
        "OMNIGENT_CONFIG_HOME": str(tmp_path / "config"),
        "OMNIGENT_AUTH_PROVIDER": "header",
        name: value,
        "UNRELATED_SECRET": "private-value",
    }
    with patch.dict(os.environ, source, clear=True):
        server_parent_env = _build_host_daemon_env(server_url=None) if via_daemon else source
    assert server_parent_env[name] == value
    if via_daemon:
        assert "UNRELATED_SECRET" not in server_parent_env
    if name != "OMNIGENT_DISABLE_TELEMETRY":
        assert "OMNIGENT_DISABLE_TELEMETRY" not in server_parent_env

    with (
        patch.dict(os.environ, server_parent_env, clear=True),
        patch.object(local_server.subprocess, "Popen") as popen,
    ):
        local_server._spawn_local_server(8765, "")
    popen.assert_called_once()
    server_env = popen.call_args.kwargs["env"]
    assert server_env[name] == value
    monkeypatch.setattr(telemetry, "_CLIENT", None)
    monkeypatch.setattr(telemetry, "_IS_DISABLED_CACHE", [None])
    with (
        patch.dict(os.environ, server_env, clear=True),
        patch.object(telemetry, "TelemetryClient") as client,
        patch("omnigent.telemetry.installation_id.get_installation_id") as installation_id,
    ):
        telemetry.init_client(config={"telemetry": True})
        assert telemetry.get_client() is None
        client.assert_not_called()
        installation_id.assert_not_called()


@pytest.mark.parametrize("server_url", [None, _REMOTE_SERVER_URL])
def test_ci_detection_stays_on_the_local_server(
    monkeypatch: pytest.MonkeyPatch,
    server_url: str | None,
) -> None:
    """CI markers reach a local server without changing runner suppression."""
    monkeypatch.setattr("omnigent.onboarding.provider_config.load_config", dict)
    with patch.dict(os.environ, {"PATH": os.defpath, "CI": "true"}, clear=True):
        daemon_env = _build_host_daemon_env(server_url=server_url)
    assert ("CI" in daemon_env) == (server_url is None)
    assert "OMNIGENT_DISABLE_TELEMETRY" not in daemon_env
    runner_env = _build_runner_env(
        daemon_env,
        server_url=server_url or "http://localhost:6767",
        runner_id="runner_ci",
        binding_token="synthetic-binding-token",
        workspace="/tmp/workspace",
        parent_pid=12345,
    )
    assert "CI" not in runner_env
    assert "OMNIGENT_DISABLE_TELEMETRY" not in runner_env


def test_gemini_gateway_survives_local_daemon_and_runner(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "fake-gateway-key")
    monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", "https://gateway.example/gemini")
    daemon = _build_host_daemon_env(server_url=None)
    runner = _build_runner_env(
        daemon,
        server_url="http://localhost:6767",
        runner_id="test-gemini",
        binding_token="fake-binding",
        workspace=str(tmp_path),
        parent_pid=os.getpid(),
    )
    for env in (daemon, runner):
        assert env["GEMINI_API_KEY"] == "fake-gateway-key"
        assert env["GOOGLE_GEMINI_BASE_URL"] == "https://gateway.example/gemini"
