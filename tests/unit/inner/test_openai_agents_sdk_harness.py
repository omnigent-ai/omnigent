"""
Tests for the ``harness: openai-agents`` wrap shape.

Mirror of the other harness wrap tests — verifies the wrap module
has the same shape (registry entry, FastAPI app routes, env-var-
driven lazy executor construction). Does NOT exercise the real
``openai-agents`` SDK; the inner ``OpenAIAgentsSDKExecutor.__init__``
is mocked so the tests pass without the package installed.

End-to-end ``openai-agents`` harness verification (real SDK, real
API) lives in the e2e suite via :mod:`tests.e2e.test_harness_wrap_e2e`.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from omnigent.inner import openai_agents_sdk_harness
from omnigent.runtime.harnesses import _HARNESS_MODULES


def test_harness_module_registered_in_module_registry() -> None:
    """``"openai-agents"`` resolves to the harness module path.

    Without this entry, the runner subprocess can't find the wrap
    when AP-side tries to spawn it for an
    ``executor.harness == "openai-agents"`` spec. The registry key
    matches the Omnigent YAML spelling (no ``-sdk`` suffix); the
    Python module name retains ``_sdk`` because the underlying
    package is ``openai-agents`` and the executor class is
    ``OpenAIAgentsSDKExecutor``.
    """
    assert _HARNESS_MODULES.get("openai-agents") == "omnigent.inner.openai_agents_sdk_harness"


def test_create_app_returns_fastapi_with_required_routes() -> None:
    """``create_app()`` returns a FastAPI app exposing the harness API.

    Verifies the wrap successfully:
    - Imports the executor adapter + OpenAI Agents SDK executor
      module.
    - Builds the FastAPI app via ExecutorAdapter.build().
    - Mounts the standard harness routes.

    The actual :class:`OpenAIAgentsSDKExecutor` is constructed
    lazily on the first turn (not at app build time), so this
    test passes without ``openai-agents`` installed.
    """
    app = openai_agents_sdk_harness.create_app()
    paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
    # Session-keyed harness API: liveness probe + single
    # discriminated-event endpoint per §The Harness API Subset.
    assert "/health" in paths
    assert "/v1/sessions/{conversation_id}/events" in paths


def test_executor_factory_reads_databricks_profile_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``HARNESS_OPENAI_AGENTS_DATABRICKS_PROFILE`` + ``_MODEL`` thread through.

    Locks in the canonical env-var contract the parametrized
    harness wrap e2e (``test_harness_wrap_e2e.py``) sets — and
    that the AP-side spawn-env builder
    (``_build_openai_agents_sdk_spawn_env`` in workflow.py)
    emits. Single canonical spelling, no ``DATABRICKS=true``
    truthy gate.
    """
    monkeypatch.setenv("HARNESS_OPENAI_AGENTS_DATABRICKS_PROFILE", "test-profile")
    monkeypatch.setenv("HARNESS_OPENAI_AGENTS_GATEWAY_HOST", "https://example.databricks.com")
    monkeypatch.setenv("HARNESS_OPENAI_AGENTS_MODEL", "databricks-gpt-5-4-mini")
    monkeypatch.setenv(
        "HARNESS_OPENAI_AGENTS_GATEWAY_BASE_URL",
        "https://example.databricks.com/ai-gateway/codex/v1",
    )
    monkeypatch.setenv("HARNESS_OPENAI_AGENTS_GATEWAY_AUTH_COMMAND", "printf token")

    captured: dict[str, Any] = {}

    def _fake_init(
        self: Any,
        *,
        client: Any = None,
        profile: str | None = None,
        api_key: str | None = None,
        use_responses: bool = True,
        model: str | None = None,
        context_window: int | None = None,
        base_url_override: str | None = None,
        gateway_host: str | None = None,
        gateway_auth_command: str | None = None,
    ) -> None:
        captured["client"] = client
        captured["profile"] = profile
        captured["api_key"] = api_key
        captured["use_responses"] = use_responses
        captured["model"] = model
        captured["base_url_override"] = base_url_override
        captured["gateway_host"] = gateway_host
        captured["gateway_auth_command"] = gateway_auth_command

    with patch(
        "omnigent.inner.openai_agents_sdk_harness.OpenAIAgentsSDKExecutor.__init__",
        _fake_init,
    ):
        openai_agents_sdk_harness._build_openai_agents_sdk_executor()

    assert captured["profile"] == "test-profile"
    assert captured["model"] == "databricks-gpt-5-4-mini"
    assert captured["gateway_host"] == "https://example.databricks.com"
    assert captured["base_url_override"] == "https://example.databricks.com/ai-gateway/codex/v1"
    assert captured["gateway_auth_command"] == "printf token"
    # ``use_responses`` defaults True when env var absent.
    assert captured["use_responses"] is True


def test_executor_factory_use_responses_default_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``use_responses`` defaults to ``True`` when the env var is unset."""
    monkeypatch.delenv("HARNESS_OPENAI_AGENTS_MODEL", raising=False)
    monkeypatch.delenv("HARNESS_OPENAI_AGENTS_USE_RESPONSES", raising=False)

    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.openai_agents_sdk_harness.OpenAIAgentsSDKExecutor.__init__",
        _fake_init,
    ):
        openai_agents_sdk_harness._build_openai_agents_sdk_executor()

    assert captured["use_responses"] is True


@pytest.mark.parametrize(
    "model",
    ["databricks-kimi-k2-6", "databricks/databricks-kimi-k2-6"],
)
def test_executor_factory_databricks_kimi_defaults_to_chat_completions(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
) -> None:
    """
    Databricks Kimi model names default to ``use_responses=False``.

    This is the compatibility path for Databricks-hosted Kimi models:
    YAML examples should only need to declare the model, not remember
    the endpoint flag. Both the bare serving endpoint name and the
    provider-qualified model spelling get the same default.
    """
    monkeypatch.setenv("HARNESS_OPENAI_AGENTS_MODEL", model)
    monkeypatch.delenv("HARNESS_OPENAI_AGENTS_USE_RESPONSES", raising=False)

    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.openai_agents_sdk_harness.OpenAIAgentsSDKExecutor.__init__",
        _fake_init,
    ):
        openai_agents_sdk_harness._build_openai_agents_sdk_executor()

    # Kimi defaults to Chat Completions; True here would route example
    # YAMLs to Responses and recreate the issue-935 failure.
    assert captured["use_responses"] is False


@pytest.mark.parametrize(
    ("model", "expected_use_responses"),
    [
        # Databricks-hosted NON-GPT models default to chat/completions: the
        # gateway only serves GPT over the Responses wire.
        ("databricks-claude-sonnet-4-6", False),
        ("databricks/databricks-claude-sonnet-4-6", False),
        ("databricks-meta-llama-3.3-70b-instruct", False),
        ("databricks-kimi-k2-6", False),
        # Databricks GPT models keep the Responses-API default.
        ("databricks-gpt-5-4-mini", True),
        ("databricks/databricks-gpt-5-5", True),
        # Non-Databricks ids keep the existing Responses default (the rule is
        # scoped to gateway-hosted models).
        ("gpt-5.5", True),
        ("qwen/qwen3.7-plus", True),
        ("claude-opus-4-8", True),
    ],
)
def test_executor_factory_non_gpt_databricks_defaults_to_chat_completions(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    expected_use_responses: bool,
) -> None:
    """
    Databricks-hosted NON-GPT models default to ``use_responses=False``.

    Generalizes the original Kimi-only carve-out: every ``databricks-*`` id
    without the ``gpt`` token is served over chat/completions by the gateway,
    so the harness derives the endpoint default from the model name. GPT and
    non-Databricks ids keep the Responses default. A wrong default here would
    point the SDK at ``/responses`` for a chat-only model and recreate the
    issue-935 failure for the whole non-GPT family, not just Kimi.
    """
    monkeypatch.setenv("HARNESS_OPENAI_AGENTS_MODEL", model)
    monkeypatch.delenv("HARNESS_OPENAI_AGENTS_USE_RESPONSES", raising=False)

    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.openai_agents_sdk_harness.OpenAIAgentsSDKExecutor.__init__",
        _fake_init,
    ):
        openai_agents_sdk_harness._build_openai_agents_sdk_executor()

    assert captured["use_responses"] is expected_use_responses


def test_executor_factory_databricks_kimi_respects_truthy_use_responses_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Explicit env config wins even for Databricks Kimi models.

    The model compatibility rule is only the default, not a silent
    override of the spec-derived ``use_responses`` setting.
    """
    monkeypatch.setenv("HARNESS_OPENAI_AGENTS_MODEL", "databricks-kimi-k2-6")
    monkeypatch.setenv("HARNESS_OPENAI_AGENTS_USE_RESPONSES", "true")

    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.openai_agents_sdk_harness.OpenAIAgentsSDKExecutor.__init__",
        _fake_init,
    ):
        openai_agents_sdk_harness._build_openai_agents_sdk_executor()

    assert captured["use_responses"] is True


@pytest.mark.parametrize(
    "raw_value,expected",
    [
        ("1", True),
        ("true", True),
        ("True", True),
        ("yes", True),
        ("0", False),
        ("false", False),
        ("anything else", False),
    ],
)
def test_use_responses_env_var_truthy_parsing(
    raw_value: str,
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``HARNESS_OPENAI_AGENTS_USE_RESPONSES`` parses truthy strings only.

    Mirrors the claude-sdk / codex / pi wraps' parsers so
    operators learn ONE set of truthy conventions, not five.
    """
    monkeypatch.delenv("HARNESS_OPENAI_AGENTS_MODEL", raising=False)
    monkeypatch.setenv("HARNESS_OPENAI_AGENTS_USE_RESPONSES", raw_value)
    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.openai_agents_sdk_harness.OpenAIAgentsSDKExecutor.__init__",
        _fake_init,
    ):
        openai_agents_sdk_harness._build_openai_agents_sdk_executor()

    assert captured["use_responses"] is expected


def test_executor_factory_no_env_returns_blank_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no HARNESS_* env vars, all overrides are ``None`` / default.

    Simulates a developer running the runner manually with their
    own ambient ``OPENAI_BASE_URL`` / ``OPENAI_API_KEY`` set. The
    wrap should pass ``profile=None`` / ``model=None`` to the
    executor so its fallback resolution path (env vars, then
    default ~/.databrickscfg profile) takes over.
    """
    for env_var in (
        "HARNESS_OPENAI_AGENTS_MODEL",
        "HARNESS_OPENAI_AGENTS_DATABRICKS_PROFILE",
        "HARNESS_OPENAI_AGENTS_USE_RESPONSES",
    ):
        monkeypatch.delenv(env_var, raising=False)

    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.openai_agents_sdk_harness.OpenAIAgentsSDKExecutor.__init__",
        _fake_init,
    ):
        openai_agents_sdk_harness._build_openai_agents_sdk_executor()

    assert captured["profile"] is None
    assert captured["model"] is None
    assert captured["use_responses"] is True
