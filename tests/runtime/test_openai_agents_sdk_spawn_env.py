"""
Tests for ``_build_openai_agents_sdk_spawn_env`` in
``omnigent/runtime/workflow.py``.

The spawn-env builder maps ``spec.executor`` fields to
``HARNESS_OPENAI_AGENTS_*`` env vars that the openai-agents harness
wrap reads at first-turn time. Mirrors the
the spawn-env pattern used by the other SDK-style harness tests.

This is a unit test — no subprocess spawn, no real httpx. End-to-end
verification of the spawn-env → wrap → runtime executor → gateway
path lives in the harness e2e tests.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml as _yaml

from omnigent.runtime.workflow import _build_openai_agents_sdk_spawn_env, _load_global_auth
from omnigent.spec.types import (
    AgentSpec,
    ApiKeyAuth,
    DatabricksAuth,
    ExecutorSpec,
    LLMConfig,
)


@pytest.fixture(autouse=True)
def _isolate_global_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    Point OMNIGENT_CONFIG_HOME at an empty temp dir for every test in
    this file so tests that don't explicitly set up a global config are
    not affected by the developer's real ``~/.omnigent/config.yaml``.

    Tests that need a specific global config write their own config.yaml
    into a separate temp dir and set OMNIGENT_CONFIG_HOME themselves —
    that setenv call wins because monkeypatch applies in call order.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))


def _make_spec(
    *,
    model: str | None = "databricks-gpt-5-4-mini",
    profile: str | None = None,
    use_responses: bool | None = None,
    auth: ApiKeyAuth | DatabricksAuth | None = None,
) -> AgentSpec:
    """
    Build a minimal openai-agents :class:`AgentSpec` for the
    spawn-env tests.

    :param model: ``spec.executor.config["model"]``; ``None`` omits
        the model from the executor config.
    :param profile: ``spec.executor.config["profile"]``; ``None``
        omits it (no profile declared in YAML).
    :param use_responses: ``spec.executor.config["use_responses"]``;
        ``None`` omits it (executor default applies).
    :param auth: Typed auth object placed on ``spec.executor.auth``;
        ``None`` omits it (harness falls back to legacy/env-var paths).
    :returns: A populated :class:`AgentSpec`.
    """
    config: dict[str, object] = {"harness": "openai-agents"}
    if model is not None:
        config["model"] = model
    if profile is not None:
        config["profile"] = profile
    if use_responses is not None:
        config["use_responses"] = use_responses
    return AgentSpec(
        spec_version=1,
        name="test-openai-agents",
        instructions="You are a test agent.",
        executor=ExecutorSpec(type="omnigent", config=config, model=model, auth=auth),
        llm=LLMConfig(model=model) if model is not None else None,
    )


def test_model_threads_into_env_var() -> None:
    """``executor.config["model"]`` is encoded into ``HARNESS_OPENAI_AGENTS_MODEL``."""
    env = _build_openai_agents_sdk_spawn_env(_make_spec(model="databricks-gpt-5-4-mini"))
    assert env["HARNESS_OPENAI_AGENTS_MODEL"] == "databricks-gpt-5-4-mini"


def test_explicit_profile_threads_into_env_var() -> None:
    """An explicit ``executor.profile`` sets ``HARNESS_OPENAI_AGENTS_DATABRICKS_PROFILE``."""
    env = _build_openai_agents_sdk_spawn_env(
        _make_spec(model="databricks-gpt-5-4-mini", profile="my-profile")
    )
    assert env["HARNESS_OPENAI_AGENTS_DATABRICKS_PROFILE"] == "my-profile"


def test_databricks_model_without_profile_gets_default_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A ``databricks-`` model with no explicit profile auto-sets
    ``HARNESS_OPENAI_AGENTS_DATABRICKS_PROFILE`` to ``"DEFAULT"``
    when ``DATABRICKS_CONFIG_PROFILE`` is not set.

    Without this, ``OPENAI_API_KEY`` in the caller's environment
    (injected by Claude Code or a prior export) short-circuits the
    executor's Databricks fallback and the request hits
    ``api.openai.com`` instead, producing a "model not found" error
    for any ``databricks-`` model name.
    """
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    env = _build_openai_agents_sdk_spawn_env(
        _make_spec(model="databricks-kimi-k2-6", profile=None)
    )
    assert env["HARNESS_OPENAI_AGENTS_DATABRICKS_PROFILE"] == "DEFAULT"


def test_databricks_slash_prefix_gets_default_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``databricks/`` provider-prefix form (LiteLLM convention) also triggers
    auto-Databricks routing when no profile is explicitly set.

    What breaks if this fails: users writing ``model: databricks/kimi-k2``
    in a harness YAML get silent routing to ``api.openai.com`` instead of
    the Databricks gateway, producing a confusing "model not found" error.
    """
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    env = _build_openai_agents_sdk_spawn_env(_make_spec(model="databricks/kimi-k2", profile=None))
    assert env["HARNESS_OPENAI_AGENTS_DATABRICKS_PROFILE"] == "DEFAULT"


def test_databricks_model_ignores_env_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Ambient ``DATABRICKS_CONFIG_PROFILE`` does NOT steer the auto-Databricks
    routing — credentials are controlled by the spec or by ``omnigent
    setup`` provider config, never by shell environment. A databricks-*
    model with no spec profile routes via the SDK ``DEFAULT`` profile.
    """
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "my-env-profile")
    env = _build_openai_agents_sdk_spawn_env(
        _make_spec(model="databricks-kimi-k2-6", profile=None)
    )
    # "DEFAULT" (not "my-env-profile") proves the env var no longer
    # controls model provisioning; "my-env-profile" here would mean the
    # removed ambient-env fallback was reintroduced.
    assert env["HARNESS_OPENAI_AGENTS_DATABRICKS_PROFILE"] == "DEFAULT"


def test_explicit_profile_wins_over_auto_default() -> None:
    """An explicit profile takes precedence over the auto-DEFAULT for ``databricks-`` models."""
    env = _build_openai_agents_sdk_spawn_env(
        _make_spec(model="databricks-kimi-k2-6", profile="staging")
    )
    assert env["HARNESS_OPENAI_AGENTS_DATABRICKS_PROFILE"] == "staging"


def test_non_databricks_model_without_profile_omits_profile_env_var() -> None:
    """Non-``databricks-`` models without a profile omit the profile env var."""
    env = _build_openai_agents_sdk_spawn_env(_make_spec(model="gpt-5.4", profile=None))
    assert "HARNESS_OPENAI_AGENTS_DATABRICKS_PROFILE" not in env


def test_use_responses_false_encodes_as_false_string() -> None:
    """``use_responses: false`` encodes as the string ``"false"``."""
    env = _build_openai_agents_sdk_spawn_env(_make_spec(use_responses=False))
    assert env["HARNESS_OPENAI_AGENTS_USE_RESPONSES"] == "false"


def test_use_responses_true_encodes_as_true_string() -> None:
    """``use_responses: true`` encodes as the string ``"true"``."""
    env = _build_openai_agents_sdk_spawn_env(_make_spec(use_responses=True))
    assert env["HARNESS_OPENAI_AGENTS_USE_RESPONSES"] == "true"


def test_use_responses_absent_omits_env_var() -> None:
    """When ``use_responses`` is unset, the env var is omitted (harness default applies)."""
    env = _build_openai_agents_sdk_spawn_env(_make_spec(use_responses=None))
    assert "HARNESS_OPENAI_AGENTS_USE_RESPONSES" not in env


def test_no_model_produces_no_model_env_var() -> None:
    """A spec with no model produces no ``HARNESS_OPENAI_AGENTS_MODEL`` env var."""
    env = _build_openai_agents_sdk_spawn_env(_make_spec(model=None))
    assert "HARNESS_OPENAI_AGENTS_MODEL" not in env


def test_profile_injects_ucode_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Profile-backed runs read OpenAI-compatible model and base URL from ucode.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.onboarding.ucode_state import UcodeAgentState, UcodeWorkspaceState

    state = UcodeWorkspaceState(
        workspace_url="https://example.databricks.com",
        claude_models={},
        codex_models=["databricks-gpt-test"],
        base_urls={"codex": "https://example.databricks.com/ai-gateway/codex/v1"},
        available_tools=["codex"],
        agents={
            "codex": UcodeAgentState(
                model="databricks-gpt-test",
                base_url="https://example.databricks.com/ai-gateway/codex/v1",
                auth_command="printf token",
            )
        },
    )
    monkeypatch.setattr(
        "omnigent.runtime.workflow.get_workspace_url_for_profile",
        lambda profile: "https://example.databricks.com",
    )
    monkeypatch.setattr(
        "omnigent.runtime.workflow.read_ucode_state",
        lambda workspace_url: state,
    )

    env = _build_openai_agents_sdk_spawn_env(_make_spec(model=None, profile="test-profile"))

    assert env["HARNESS_OPENAI_AGENTS_MODEL"] == "databricks-gpt-test"
    assert (
        env["HARNESS_OPENAI_AGENTS_GATEWAY_BASE_URL"]
        == "https://example.databricks.com/ai-gateway/codex/v1"
    )
    assert env["HARNESS_OPENAI_AGENTS_GATEWAY_HOST"] == "https://example.databricks.com"
    assert env["HARNESS_OPENAI_AGENTS_GATEWAY_AUTH_COMMAND"] == "printf token"


# ---------------------------------------------------------------------------
# executor.auth tests
# ---------------------------------------------------------------------------


def test_databricks_auth_sets_profile_env_var() -> None:
    """
    ``executor.auth: {type: databricks, profile: oss}`` sets
    ``HARNESS_OPENAI_AGENTS_DATABRICKS_PROFILE`` and does NOT set the
    API key env var.

    Failure means DatabricksAuth on the spec is silently dropped and
    the harness falls back to env-var auth instead of the explicit profile.
    """
    spec = _make_spec(
        model="databricks-gpt-5-4-mini",
        auth=DatabricksAuth(profile="oss"),
    )
    env = _build_openai_agents_sdk_spawn_env(spec)

    assert env["HARNESS_OPENAI_AGENTS_DATABRICKS_PROFILE"] == "oss"
    assert "HARNESS_OPENAI_AGENTS_API_KEY" not in env


def test_api_key_auth_sets_api_key_env_var() -> None:
    """
    ``executor.auth: {type: api_key, api_key: sk-test}`` sets
    ``HARNESS_OPENAI_AGENTS_API_KEY`` and does NOT set the Databricks
    profile env var.

    Failure means ApiKeyAuth on the spec is silently dropped and the
    harness falls back to ambient OPENAI_API_KEY resolution instead.
    """
    spec = _make_spec(
        model="gpt-4o",
        auth=ApiKeyAuth(api_key="sk-test-456"),
    )
    env = _build_openai_agents_sdk_spawn_env(spec)

    assert env["HARNESS_OPENAI_AGENTS_API_KEY"] == "sk-test-456"
    assert "HARNESS_OPENAI_AGENTS_DATABRICKS_PROFILE" not in env


def test_spec_auth_takes_precedence_over_global_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When the spec declares ``executor.auth``, the global config auth
    block is ignored.

    Failure means per-spec auth is silently overridden by the global
    config, breaking spec self-containment.
    """
    with tempfile.TemporaryDirectory() as td:
        cfg_path = Path(td) / "config.yaml"
        cfg_path.write_text(
            _yaml.dump({"auth": {"type": "databricks", "profile": "global-profile"}})
        )
        monkeypatch.setenv("OMNIGENT_CONFIG_HOME", td)

        spec = _make_spec(
            model="databricks-gpt-5-4-mini",
            auth=DatabricksAuth(profile="spec-profile"),
        )
        env = _build_openai_agents_sdk_spawn_env(spec)

    # Spec-level profile must win over global config profile.
    assert env["HARNESS_OPENAI_AGENTS_DATABRICKS_PROFILE"] == "spec-profile"


def test_global_config_auth_used_when_spec_auth_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When the spec has no ``executor.auth``, the global config ``auth:``
    block provides the fallback credentials.

    Failure means users must declare auth in every agent YAML and
    cannot rely on the once-configured global default from
    ``omnigent setup``.
    """
    with tempfile.TemporaryDirectory() as td:
        cfg_path = Path(td) / "config.yaml"
        cfg_path.write_text(
            _yaml.dump({"auth": {"type": "databricks", "profile": "global-profile"}})
        )
        monkeypatch.setenv("OMNIGENT_CONFIG_HOME", td)
        monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)

        spec = _make_spec(model="databricks-gpt-5-4-mini", auth=None, profile=None)
        env = _build_openai_agents_sdk_spawn_env(spec)

    assert env["HARNESS_OPENAI_AGENTS_DATABRICKS_PROFILE"] == "global-profile"


def test_load_global_auth_databricks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``_load_global_auth()`` returns a :class:`DatabricksAuth` when the
    config file has ``auth: {type: databricks, profile: …}``.
    """
    with tempfile.TemporaryDirectory() as td:
        cfg_path = Path(td) / "config.yaml"
        cfg_path.write_text(_yaml.dump({"auth": {"type": "databricks", "profile": "my-profile"}}))
        monkeypatch.setenv("OMNIGENT_CONFIG_HOME", td)
        result = _load_global_auth()

    assert isinstance(result, DatabricksAuth)
    assert result.profile == "my-profile"


def test_load_global_auth_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``_load_global_auth()`` returns an :class:`ApiKeyAuth` when the
    config file has ``auth: {type: api_key, api_key: …}`` and expands
    env-var references.
    """
    monkeypatch.setenv("MY_GLOBAL_KEY", "sk-global-999")
    with tempfile.TemporaryDirectory() as td:
        cfg_path = Path(td) / "config.yaml"
        cfg_path.write_text(_yaml.dump({"auth": {"type": "api_key", "api_key": "$MY_GLOBAL_KEY"}}))
        monkeypatch.setenv("OMNIGENT_CONFIG_HOME", td)
        result = _load_global_auth()

    assert isinstance(result, ApiKeyAuth)
    assert result.api_key == "sk-global-999"


def test_global_config_auth_not_applied_when_spec_has_legacy_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When the spec declares a profile via the legacy ``executor.config["profile"]``
    path, the global config ``auth:`` block is ignored.

    Failure means a YAML like ``executor.profile: oss`` silently has its
    Databricks profile overridden by an api_key in the user's global
    config — the agent then hits ``api.openai.com`` instead of the
    Databricks gateway and gets an auth error.
    """
    with tempfile.TemporaryDirectory() as td:
        cfg_path = Path(td) / "config.yaml"
        # Global config has api_key auth — should NOT apply when spec has a profile.
        cfg_path.write_text(_yaml.dump({"auth": {"type": "api_key", "api_key": "sk-global"}}))
        monkeypatch.setenv("OMNIGENT_CONFIG_HOME", td)
        monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)

        # Spec declares profile via the legacy config dict (omnigent compat path).
        spec = _make_spec(model="databricks-gpt-5-4-mini", profile="oss", auth=None)
        env = _build_openai_agents_sdk_spawn_env(spec)

    # Legacy profile must be used; api_key from global config must be absent.
    assert env["HARNESS_OPENAI_AGENTS_DATABRICKS_PROFILE"] == "oss"
    assert "HARNESS_OPENAI_AGENTS_API_KEY" not in env


def test_load_global_auth_missing_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_load_global_auth()`` returns ``None`` when no config file exists."""
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("OMNIGENT_CONFIG_HOME", td)
        result = _load_global_auth()

    assert result is None


def test_load_global_auth_api_key_with_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``_load_global_auth()`` parses ``base_url`` from the global config
    and expands env-var references in it.

    Failure means a user who configures a custom endpoint in
    ``~/.omnigent/config.yaml`` via an env-var reference has the
    literal ``$VAR`` string passed as the base URL.
    """
    monkeypatch.setenv("MY_GLOBAL_KEY", "sk-global-abc")
    monkeypatch.setenv("MY_BASE_URL", "https://my-gateway.example.com/v1")
    with tempfile.TemporaryDirectory() as td:
        cfg_path = Path(td) / "config.yaml"
        cfg_path.write_text(
            _yaml.dump(
                {
                    "auth": {
                        "type": "api_key",
                        "api_key": "$MY_GLOBAL_KEY",
                        "base_url": "$MY_BASE_URL",
                    }
                }
            )
        )
        monkeypatch.setenv("OMNIGENT_CONFIG_HOME", td)
        result = _load_global_auth()

    assert isinstance(result, ApiKeyAuth)
    assert result.api_key == "sk-global-abc"
    assert result.base_url == "https://my-gateway.example.com/v1"


def test_load_global_auth_unresolved_env_var_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``_load_global_auth()`` raises when ``api_key`` contains an unresolved
    ``$VAR`` reference (the env var is not set).

    Failure means a config with ``api_key: $MISSING_KEY`` silently passes
    the literal ``$MISSING_KEY`` string to the API, producing a confusing
    401 "invalid API key" error rather than a clear configuration error.
    """
    from omnigent.errors import OmnigentError

    monkeypatch.delenv("MISSING_KEY", raising=False)
    with tempfile.TemporaryDirectory() as td:
        cfg_path = Path(td) / "config.yaml"
        cfg_path.write_text(_yaml.dump({"auth": {"type": "api_key", "api_key": "$MISSING_KEY"}}))
        monkeypatch.setenv("OMNIGENT_CONFIG_HOME", td)

        with pytest.raises(OmnigentError):
            _load_global_auth()


def test_api_key_auth_base_url_sets_base_url_env_var() -> None:
    """
    ``executor.auth: {type: api_key, base_url: …}`` writes
    ``HARNESS_OPENAI_AGENTS_GATEWAY_BASE_URL`` alongside the API key.

    Failure means the custom endpoint declared in the spec is silently
    dropped and the executor uses the default OpenAI endpoint instead.
    """
    spec = _make_spec(
        model="gpt-4o",
        auth=ApiKeyAuth(api_key="sk-test-789", base_url="https://my-gw.example.com/v1"),
    )
    env = _build_openai_agents_sdk_spawn_env(spec)

    assert env["HARNESS_OPENAI_AGENTS_API_KEY"] == "sk-test-789"
    assert env["HARNESS_OPENAI_AGENTS_GATEWAY_BASE_URL"] == "https://my-gw.example.com/v1"


def test_api_key_auth_without_base_url_omits_base_url_env_var() -> None:
    """
    When ``executor.auth.base_url`` is absent, the base-URL env var is
    not written so the executor uses the default OpenAI endpoint.
    """
    spec = _make_spec(model="gpt-4o", auth=ApiKeyAuth(api_key="sk-test-000"))
    env = _build_openai_agents_sdk_spawn_env(spec)

    assert env["HARNESS_OPENAI_AGENTS_API_KEY"] == "sk-test-000"
    assert "HARNESS_OPENAI_AGENTS_GATEWAY_BASE_URL" not in env


def test_telemetry_env_injected_when_configured_and_span_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Issue #1051: with OTLP configured and an active omnigent span,
    the openai-agents builder injects ``TRACEPARENT`` plus the OTLP
    exporter knobs so the SDK's provider spans nest under the
    omnigent agent span. The Claude SDK-specific flags must NOT be
    set — the openai-agents SDK doesn't read them.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")

    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider

    # Use a fresh OTel TracerProvider directly so the active-span
    # context is guaranteed regardless of MLflow tracing init state.
    provider = TracerProvider()
    otel_trace._TRACER_PROVIDER = provider  # type: ignore[attr-defined]
    otel_trace._TRACER_PROVIDER_SET_ONCE._done = True  # type: ignore[attr-defined]
    tracer = otel_trace.get_tracer("omnigent.test")

    with tracer.start_as_current_span("agent"):
        env = _build_openai_agents_sdk_spawn_env(
            _make_spec(model="gpt-4o", auth=ApiKeyAuth(api_key="sk-test"))
        )

    assert "TRACEPARENT" in env
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://localhost:4317"
    assert "CLAUDE_CODE_ENABLE_TELEMETRY" not in env
    assert "OTEL_TRACES_EXPORTER" not in env


def test_no_telemetry_env_leaks_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Without ``OTEL_EXPORTER_OTLP_ENDPOINT``, no OTel keys leak into
    the openai-agents spawn env.
    """
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

    env = _build_openai_agents_sdk_spawn_env(
        _make_spec(model="gpt-4o", auth=ApiKeyAuth(api_key="sk-test"))
    )

    for key in ("TRACEPARENT", "OTEL_EXPORTER_OTLP_ENDPOINT"):
        assert key not in env, f"unexpected {key!r} in spawn env: {env!r}"
