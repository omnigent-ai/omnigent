"""Unit tests for the bench env resolver (derive creds like ``omni run``).

Network-free: monkeypatches the config lookup and the canonical Databricks
resolver, so these assert the *layering* (ambient wins, ``--profile`` overrides
config, no-creds skips) without touching ``~/.databrickscfg`` or the gateway.
"""

from __future__ import annotations

import pytest

from tests.harness_bench import runtime_env
from tests.harness_bench.runtime_env import (
    BenchRuntimeEnv,
    bench_creds_skip_reason,
    resolve_bench_env,
)

# Captured before the autouse _clean_env fixture stubs the module attribute, so
# the tier-3 test can drive the real resolver.
_REAL_PROFILE_FROM_CONFIG = runtime_env._profile_from_config


class _Creds:
    """Stand-in for ``WorkspaceCreds`` (host + token)."""

    def __init__(self, host: str, token: str) -> None:
        self.host = host
        self.token = token


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start each test from a known env: no ambient OPENAI_*, no config profile."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    monkeypatch.setattr(runtime_env, "_profile_from_config", lambda: None)


def _stub_resolver(monkeypatch: pytest.MonkeyPatch, host: str, token: str) -> None:
    monkeypatch.setattr(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace",
        lambda profile: _Creds(host, token),
    )


def test_explicit_profile_mints_via_canonical_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_resolver(monkeypatch, "https://ws.example.com", "tok-123")
    env = resolve_bench_env("oss")
    assert isinstance(env, BenchRuntimeEnv)
    assert env.db_profile == "oss"
    assert env.base_env["OPENAI_BASE_URL"] == "https://ws.example.com/serving-endpoints"
    assert env.base_env["OPENAI_API_KEY"] == "tok-123"
    assert env.base_env["DATABRICKS_CONFIG_PROFILE"] == "oss"


def test_ambient_openai_wins_and_skips_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://ambient.example.com/serving-endpoints")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-key")

    def _boom(profile: str | None) -> _Creds:  # pragma: no cover - must not run
        raise AssertionError("resolver must not be called when ambient OPENAI_* is set")

    monkeypatch.setattr(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace", _boom
    )
    env = resolve_bench_env(None)
    assert env.base_env["OPENAI_API_KEY"] == "ambient-key"
    assert env.base_env["OPENAI_BASE_URL"] == "https://ambient.example.com/serving-endpoints"


def test_config_profile_used_when_no_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    # No --profile, but ~/.omnigent config yields a profile (like `omni run`).
    monkeypatch.setattr(runtime_env, "_profile_from_config", lambda: "from-config")
    seen: dict[str, str | None] = {}

    def _resolver(profile: str | None) -> _Creds:
        seen["profile"] = profile
        return _Creds("https://cfg.example.com", "cfg-tok")

    monkeypatch.setattr(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace", _resolver
    )
    env = resolve_bench_env(None)
    assert seen["profile"] == "from-config"
    assert env.db_profile == "from-config"
    assert env.base_env["OPENAI_API_KEY"] == "cfg-tok"


def test_explicit_profile_overrides_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_env, "_profile_from_config", lambda: "from-config")
    seen: dict[str, str | None] = {}

    def _resolver(profile: str | None) -> _Creds:
        seen["profile"] = profile
        return _Creds("https://x", "t")

    monkeypatch.setattr(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace", _resolver
    )
    resolve_bench_env("explicit")
    assert seen["profile"] == "explicit"  # flag wins over config


def test_profile_from_providers_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 3: with no ``auth:`` / top-level ``profile:``, the default
    ``providers:`` databricks entry supplies the profile — the common
    provider-wizard config where ``omni run`` goes live with no ``--profile``.

    Exercises the real ``_profile_from_config`` (captured before the autouse
    stub) by monkeypatching the two upstream config sources it reads.
    """
    monkeypatch.setattr("omnigent.runtime.workflow._load_global_auth", lambda: None)
    monkeypatch.setattr(
        "omnigent.cli._load_effective_config",
        lambda: {
            "providers": {
                "databricks": {"kind": "databricks", "default": True, "profile": "DEFAULT"}
            }
        },
    )
    assert _REAL_PROFILE_FROM_CONFIG() == "DEFAULT"


def test_skip_reason_none_when_ambient(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://a/serving-endpoints")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    assert bench_creds_skip_reason(None) is None


def test_skip_reason_when_no_creds_anywhere() -> None:
    # _clean_env stubbed _profile_from_config -> None and cleared OPENAI_*.
    reason = bench_creds_skip_reason(None)
    assert reason is not None
    assert "--profile" in reason


def test_skip_reason_when_profile_hostless(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tests.e2e.helpers.lookup_databricks_host", lambda p: None)
    reason = bench_creds_skip_reason("typo-profile")
    assert reason is not None
    assert "typo-profile" in reason
