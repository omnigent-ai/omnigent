"""Tests for omnigent.runtime.credentials.databricks."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from omnigent.runtime.credentials.databricks import (
    WorkspaceCreds,
    resolve_databricks_workspace,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Strip every credential-related env var before each test so the
    resolver cannot accidentally pick up the developer's real
    credentials, and short-circuit the SDK path so tests don't
    perform real OAuth / network authentication.

    Why the SDK short-circuit: ``databricks.sdk.config.Config(...)
    .authenticate()`` walks every supported auth_type — PAT, OAuth-
    U2M, Azure CLI, OIDC, IMDS, etc. Several of those make
    network calls or shell out to external CLIs even when the
    resolver intends to fall through to the cfg-file path. Without
    this monkeypatch the suite takes ~35 minutes; with it, ~0.4s.
    Each test that wants to exercise the SDK path explicitly
    overrides this monkeypatch (see ``test_resolves_via_sdk_*``).
    """
    for var in (
        "DATABRICKS_CONFIG_FILE",
        "DATABRICKS_CONFIG_PROFILE",
    ):
        monkeypatch.delenv(var, raising=False)

    def _raise_value_error(*_args: object, **_kwargs: object) -> None:
        raise ValueError("SDK path disabled in tests by default")

    monkeypatch.setattr("databricks.sdk.config.Config", _raise_value_error)


def _write_cfg(tmp_path: Path, body: str) -> Path:
    """
    Write *body* to a temporary ``.databrickscfg`` and return its path.

    :param tmp_path: pytest's per-test temp directory.
    :param body: The exact INI text to write.
    :returns: The :class:`Path` to the written config file.
    """
    cfg = tmp_path / "databrickscfg"
    cfg.write_text(body)
    return cfg


def test_openai_env_vars_are_ignored(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # OPENAI_BASE_URL / OPENAI_API_KEY are used elsewhere in the
    # codebase to point at full serving-endpoints URLs; this resolver
    # MUST NOT treat them as workspace creds. If it did, a downstream
    # caller appending the gateway path would produce a malformed URL
    # like .../serving-endpoints/ai-gateway/mlflow/v1.
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.com/serving-endpoints")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-token")
    cfg = _write_cfg(
        tmp_path,
        "[DEFAULT]\nhost = https://default.example.com\ntoken = default-token\n",
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))

    creds = resolve_databricks_workspace(profile=None)

    # Resolver ignored the OPENAI_* env vars and went straight to the
    # cfg file. If this returned the OpenAI host instead, the resolver
    # is silently mis-using OpenAI env vars as workspace credentials.
    assert creds.host == "https://default.example.com"
    assert creds.token == "default-token"


def test_resolves_named_profile_from_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _write_cfg(
        tmp_path,
        ("[dev]\nhost = https://dev.example.com\ntoken = dev-token\n"),
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))

    creds = resolve_databricks_workspace(profile="dev")

    # Named-profile branch returned the [dev] section's exact values.
    assert creds == WorkspaceCreds(host="https://dev.example.com", token="dev-token")


def test_resolves_default_when_profile_is_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = _write_cfg(
        tmp_path,
        ("[DEFAULT]\nhost = https://default.example.com\ntoken = default-token\n"),
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))

    creds = resolve_databricks_workspace(profile=None)

    # profile=None went straight to [DEFAULT] and pulled both values.
    assert creds == WorkspaceCreds(host="https://default.example.com", token="default-token")


def test_named_profile_overrides_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _write_cfg(
        tmp_path,
        (
            "[DEFAULT]\n"
            "host = https://default.example.com\n"
            "token = default-token\n"
            "\n"
            "[dev]\n"
            "host = https://dev.example.com\n"
            "token = dev-token\n"
        ),
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))

    creds = resolve_databricks_workspace(profile="dev")

    # When the named profile exists AND has both fields, it wins over
    # [DEFAULT]. If this returned the DEFAULT host instead, the
    # resolver is silently ignoring the requested profile.
    assert creds.host == "https://dev.example.com"
    assert creds.token == "dev-token"


def test_absent_named_profile_raises_not_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # [DEFAULT] has valid creds but [ghost] doesn't exist. The resolver
    # must raise OSError rather than silently routing to DEFAULT — a
    # typo in --profile would otherwise send requests to a completely
    # different workspace without any error.
    cfg = _write_cfg(
        tmp_path,
        ("[DEFAULT]\nhost = https://default.example.com\ntoken = default-token\n"),
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))

    with pytest.raises(OSError) as excinfo:
        resolve_databricks_workspace(profile="ghost")

    msg = str(excinfo.value)
    assert "ghost" in msg
    assert str(cfg) in msg


def test_databricks_config_profile_env_var_typo_raises_not_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Simulates: `omnigent run foo.yaml --profile typo-profile --model databricks/m`
    # _propagate_profile_to_environment sets DATABRICKS_CONFIG_PROFILE="typo-profile".
    # DatabricksAdapter calls resolve_databricks_workspace(None).
    # Without the effective_profile fix, the configparser path receives profile=None,
    # skips the named-section check, and silently returns [DEFAULT] creds.
    cfg = _write_cfg(
        tmp_path,
        "[DEFAULT]\nhost = https://default.example.com\ntoken = default-token\n",
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "typo-profile")

    with pytest.raises(OSError) as excinfo:
        resolve_databricks_workspace(profile=None)

    msg = str(excinfo.value)
    assert "typo-profile" in msg
    assert str(cfg) in msg


def test_strips_trailing_slash_from_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _write_cfg(
        tmp_path,
        ("[dev]\nhost = https://dev.example.com///\ntoken = dev-token\n"),
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))

    creds = resolve_databricks_workspace(profile="dev")

    # Three trailing slashes in the cfg, zero in the result. Callers
    # rely on this normalization to use the host directly as an
    # OpenAI base_url without further cleanup.
    assert creds.host == "https://dev.example.com"


def test_raises_when_all_sources_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Point DATABRICKS_CONFIG_FILE at a path that does not exist so
    # the resolver cannot accidentally pick up the developer's real
    # ~/.databrickscfg. The autouse fixture has already disabled the
    # SDK path.
    missing_cfg = tmp_path / "does-not-exist"
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(missing_cfg))

    with pytest.raises(OSError) as excinfo:
        resolve_databricks_workspace(profile="dev")

    msg = str(excinfo.value)
    # The error must name every source that was checked so the
    # caller can debug exactly which piece is missing.
    assert "databricks-sdk" in msg
    assert "[dev]" in msg or "profile [dev]" in msg
    assert "[DEFAULT]" in msg
    assert str(missing_cfg) in msg


def test_raises_when_cfg_section_missing_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = _write_cfg(
        tmp_path,
        (
            "[dev]\nhost = https://dev.example.com\n"
            # NOTE: no token line
        ),
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))

    # The [dev] section exists but is missing the token. The resolver
    # must NOT silently substitute an empty token — it must treat the
    # section as unresolved and (since [DEFAULT] is also missing)
    # raise OSError.
    with pytest.raises(OSError):
        resolve_databricks_workspace(profile="dev")


def test_malformed_named_section_does_not_silently_fall_back_to_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Two sections exist: a malformed [dev] (missing token) and a
    # complete [DEFAULT]. The resolver must NOT silently use DEFAULT
    # when the user explicitly asked for [dev] — that would send the
    # caller to a different workspace than they requested, which is
    # very hard to debug because everything "works" but talks to
    # the wrong place.
    cfg = _write_cfg(
        tmp_path,
        (
            "[DEFAULT]\n"
            "host  = https://default.example.com\n"
            "token = default-token\n"
            "\n"
            "[dev]\n"
            "host  = https://dev.example.com\n"
            # NOTE: no token line — section present but invalid
        ),
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))

    with pytest.raises(OSError) as excinfo:
        resolve_databricks_workspace(profile="dev")

    msg = str(excinfo.value)
    # Error must name the offending profile so the user knows which
    # section to fix.
    assert "[dev]" in msg
    # Error should mention 'malformed' or what's missing so the user
    # can debug fast.
    assert "malformed" in msg.lower() or "token" in msg


def test_oauth_profile_without_token_is_not_malformed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An OAuth profile (auth_type = databricks-cli) legitimately has no
    # static ``token`` — only the SDK path can mint one. When that path
    # fails (autouse fixture forces the SDK to raise), the configparser
    # fallback must NOT report the profile as "malformed" and tell the
    # user to fix/remove it. It should raise an actionable error that
    # points at the SDK path / OAuth session instead.
    cfg = _write_cfg(
        tmp_path,
        (
            "[oss]\nhost = https://oss.example.com\nauth_type = databricks-cli\n"
            # NOTE: no token line — expected for OAuth
        ),
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))

    # SDK is importable in the test env but the autouse fixture forces
    # authenticate() to fail → the "installed but auth failed" branch.
    with pytest.raises(OSError) as excinfo:
        resolve_databricks_workspace(profile="oss")

    msg = str(excinfo.value)
    assert "[oss]" in msg
    # Must NOT defame a valid OAuth profile as malformed.
    assert "malformed" not in msg.lower()
    # Should steer the user toward the real fix (SDK / OAuth session).
    assert "OAuth" in msg
    assert "databricks auth login" in msg


def test_oauth_profile_error_recommends_extra_when_sdk_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # When databricks-sdk is not installed (base install, no extra),
    # the OAuth profile is unresolvable and the error must tell the user
    # to install the `databricks` extra — not to mess with their CLI /
    # OAuth session, which are irrelevant when the package is missing.
    cfg = _write_cfg(
        tmp_path,
        "[oss]\nhost = https://oss.example.com\nauth_type = databricks-cli\n",
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))
    # Simulate the base install: the SDK package is not importable.
    monkeypatch.setattr(
        "omnigent.runtime.credentials.databricks._databricks_sdk_importable",
        lambda: False,
    )

    with pytest.raises(OSError) as excinfo:
        resolve_databricks_workspace(profile="oss")

    msg = str(excinfo.value)
    assert "[oss]" in msg
    assert "malformed" not in msg.lower()
    # Points at the missing package / extra, not the CLI/login session.
    assert "omnigent[databricks]" in msg
    assert "databricks auth login" not in msg


def test_non_cli_sdk_profile_does_not_recommend_databricks_auth_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A token-less SDK-only profile whose auth_type is NOT databricks-cli
    # (e.g. azure-cli) must not be told to run `databricks auth login` —
    # that command only refreshes databricks-cli OAuth-U2M sessions. The
    # SDK is importable in the test env; the autouse fixture forces auth
    # to fail → the "SDK present but auth failed" branch.
    cfg = _write_cfg(
        tmp_path,
        "[az]\nhost = https://az.example.com\nauth_type = azure-cli\n",
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))

    with pytest.raises(OSError) as excinfo:
        resolve_databricks_workspace(profile="az")

    msg = str(excinfo.value)
    assert "[az]" in msg
    assert "malformed" not in msg.lower()
    # Names the actual auth_type; does NOT misdirect to the CLI login.
    assert "azure-cli" in msg
    assert "databricks auth login" not in msg


def test_resolves_via_sdk_when_sdk_returns_creds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Override the autouse fixture's SDK short-circuit so this test
    # exercises the SDK branch end-to-end (with a fake Config that
    # returns plausible OAuth-style results — proves the resolver
    # works for ``auth_type = databricks-cli`` profiles whose cfg
    # sections have NO static ``token`` field).
    class _FakeConfig:
        def __init__(self, *, profile: str | None) -> None:
            self.host = "https://sdk.example.com/"  # trailing slash on purpose

        def authenticate(self) -> dict[str, str]:
            return {"Authorization": "Bearer sdk-minted-token"}

    monkeypatch.setattr("databricks.sdk.config.Config", _FakeConfig)

    creds = resolve_databricks_workspace(profile="some-oauth-profile")

    # SDK path returned a freshly-minted bearer; trailing slash on
    # the host was stripped on the way out.
    assert creds == WorkspaceCreds(host="https://sdk.example.com", token="sdk-minted-token")


def test_sdk_failure_falls_through_to_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Autouse fixture already sets Config to raise ValueError. Provide
    # a cfg file with a plain-PAT [dev] section. The resolver must
    # catch the SDK ValueError and fall through to the configparser
    # path rather than propagating the failure.
    cfg = _write_cfg(
        tmp_path,
        "[dev]\nhost = https://cfg-fallback.example.com\ntoken = cfg-token\n",
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))

    creds = resolve_databricks_workspace(profile="dev")

    # If this returned None or raised, the resolver isn't catching
    # the SDK's ValueError properly — the cfg-file fallback would
    # never run for any setup where the SDK initialization fails.
    assert creds.host == "https://cfg-fallback.example.com"
    assert creds.token == "cfg-token"


def test_sdk_non_bearer_auth_falls_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Some auth schemes (Basic, etc.) return non-Bearer Authorization
    # headers. The resolver doesn't support those — it must fall
    # through to the cfg-file path rather than returning a malformed
    # token.
    class _NonBearerConfig:
        def __init__(self, *, profile: str | None) -> None:
            self.host = "https://sdk.example.com"

        def authenticate(self) -> dict[str, str]:
            return {"Authorization": "Basic some-base64-blob"}

    monkeypatch.setattr("databricks.sdk.config.Config", _NonBearerConfig)
    cfg = _write_cfg(
        tmp_path,
        "[dev]\nhost = https://cfg.example.com\ntoken = cfg-token\n",
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))

    creds = resolve_databricks_workspace(profile="dev")

    # SDK returned Basic auth (unsupported) → resolver fell through
    # to the cfg path. If this returned an "sdk.example.com" host,
    # the resolver is silently accepting non-Bearer schemes.
    assert creds.host == "https://cfg.example.com"


def test_sdk_value_error_does_not_emit_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Regression: SDK ValueError used to log at WARNING with
    # exc_info=True, dumping a 40-line traceback ahead of the clean
    # ClickException. INFO keeps it in cli-*.log but off stderr.
    cfg = _write_cfg(
        tmp_path,
        "[dev]\nhost = https://cfg.example.com\ntoken = cfg-token\n",
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))

    with caplog.at_level(logging.DEBUG, logger="omnigent.runtime.credentials.databricks"):
        resolve_databricks_workspace(profile="dev")

    module_records = [
        r for r in caplog.records if r.name == "omnigent.runtime.credentials.databricks"
    ]
    # WARNING+ would re-introduce the stderr traceback.
    warnings_or_louder = [r for r in module_records if r.levelno >= logging.WARNING]
    assert warnings_or_louder == [], (
        f"Expected no WARNING+ records, got "
        f"{[(r.levelname, r.getMessage()) for r in warnings_or_louder]}."
    )

    # INFO record preserves the diagnostic for cli-*.log post-mortem.
    info_records = [
        r
        for r in module_records
        if r.levelno == logging.INFO and "Config(profile=" in r.getMessage()
    ]
    assert len(info_records) == 1, (
        f"Expected one INFO record, got {len(info_records)}. "
        f"Records: {[(r.levelname, r.getMessage()) for r in module_records]}."
    )
    # exc_info is (type, value, traceback); [2] must be real, else the
    # frames won't render in the log.
    exc_info = info_records[0].exc_info
    assert exc_info is not None and exc_info[2] is not None, (
        "INFO record must carry exc_info with a real traceback."
    )


def test_honors_databricks_config_file_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Two cfg files in different locations; we point
    # DATABRICKS_CONFIG_FILE at the second one and expect that one to
    # be used. This proves the env var overrides the default
    # ~/.databrickscfg path.
    decoy_dir = tmp_path / "decoy"
    decoy_dir.mkdir()
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    decoy = _write_cfg(
        decoy_dir,
        "[DEFAULT]\nhost = https://decoy.example.com\ntoken = decoy-token\n",
    )
    real = _write_cfg(
        real_dir,
        "[DEFAULT]\nhost = https://real.example.com\ntoken = real-token\n",
    )
    assert decoy.exists()  # sanity check: both files were written
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(real))

    creds = resolve_databricks_workspace(profile=None)

    # The resolver read from `real`, not `decoy`. If this assertion
    # fails on the decoy host, the env var override is broken.
    assert creds.host == "https://real.example.com"
    assert creds.token == "real-token"
