"""Codex provider homes stay absolute, private, and runner-visible."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.errors import OmnigentError
from omnigent.host.connect import _build_runner_env
from omnigent.onboarding.provider_config import (
    load_providers,
    provider_cli_home,
    provider_credential_env_vars,
)
from omnigent.onboarding.provider_inventory import (
    CapabilitySupport,
    ConnectionState,
    provider_capabilities,
    provider_connection_state,
)


def _config(home: object = None, *, cli: str = "codex") -> dict[str, object]:
    provider: dict[str, object] = {"kind": "subscription", "cli": cli}
    if home is not None:
        provider["cli_home"] = home
    return {"providers": {"work": provider}}


def test_cli_home_expands_and_normalizes_an_absolute_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "accounts"
    monkeypatch.setenv("CODEX_ACCOUNT_ROOT", str(root))
    [provider] = load_providers(_config("$CODEX_ACCOUNT_ROOT/../accounts/work")).values()

    assert provider.cli_home == "$CODEX_ACCOUNT_ROOT/../accounts/work"
    assert provider_cli_home(provider) == root / "work"


def test_cli_home_expands_the_current_users_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    [provider] = load_providers(_config("~/.codex-work")).values()

    assert provider_cli_home(provider) == tmp_path / ".codex-work"


def test_cli_home_rejects_relative_and_unresolved_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    [relative] = load_providers(_config("profiles/work")).values()
    with pytest.raises(OmnigentError, match="absolute path"):
        provider_cli_home(relative)

    monkeypatch.delenv("MISSING_CODEX_ACCOUNT_ROOT", raising=False)
    [unresolved] = load_providers(_config("$MISSING_CODEX_ACCOUNT_ROOT/work")).values()
    with pytest.raises(OmnigentError, match="Unresolved environment variable"):
        provider_cli_home(unresolved)


@pytest.mark.parametrize("value", ["", "   ", 5, []])
def test_cli_home_must_be_a_non_empty_string(value: object) -> None:
    with pytest.raises(OmnigentError, match="non-empty path"):
        load_providers(_config(value))


def test_cli_home_is_rejected_for_claude_until_its_launch_honors_it() -> None:
    with pytest.raises(OmnigentError, match="supported only for cli: 'codex'"):
        load_providers(_config("/tmp/claude-work", cli="claude"))


def test_cli_home_environment_is_forwarded_to_the_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config("$CODEX_ACCOUNT_ROOT/work")
    monkeypatch.setattr("omnigent.onboarding.provider_config.load_config", lambda: config)
    root = str(tmp_path / "accounts")
    base_env = {"CODEX_ACCOUNT_ROOT": root}

    env = _build_runner_env(
        base_env,
        server_url="http://127.0.0.1:8000",
        runner_id="runner_test",
        binding_token="binding-token",
        workspace=str(tmp_path),
        parent_pid=123,
    )

    assert provider_credential_env_vars(config) >= {
        "CODEX_ACCOUNT_ROOT",
        "OMNIGENT_CODEX_ACCOUNT_ROOT",
    }
    assert env["CODEX_ACCOUNT_ROOT"] == root


def test_inventory_does_not_claim_general_codex_profile_support() -> None:
    [provider] = load_providers(_config("/tmp/codex-work")).values()

    assert provider_capabilities(provider).multiple_profiles is CapabilitySupport.UNSUPPORTED


def test_selected_home_readiness_ignores_ambient_profile_evidence(tmp_path: Path) -> None:
    [provider] = load_providers(_config(str(tmp_path / "codex-work"))).values()

    connection = provider_connection_state(
        provider,
        harness_readiness={"codex-native": "needs-auth"},
        provider_detected=True,
    )

    assert connection.state is ConnectionState.UNKNOWN
    assert "selected Codex home" in connection.detail


def test_selected_subscription_home_is_connected_only_with_safe_local_proof(
    tmp_path: Path,
) -> None:
    work_home = tmp_path / "codex-work"
    work_home.mkdir()
    (work_home / "auth.json").write_text('{"OPENAI_API_KEY":"not-a-real-secret"}')
    [provider] = load_providers(_config(str(work_home))).values()

    connection = provider_connection_state(provider, provider_detected=False)

    assert connection.state is ConnectionState.CONNECTED
    assert "not-a-real-secret" not in connection.detail
