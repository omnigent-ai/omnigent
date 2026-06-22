"""
Tests for the ``harness: codex`` wrap shape.

Mirror of ``tests/inner/test_claude_sdk_harness.py`` — verifies
the wrap module has the same shape (registry entry, FastAPI app
routes, env-var-driven lazy executor construction). Does NOT
exercise the real Codex CLI; the inner ``CodexExecutor.__init__``
is mocked so the test passes without a ``codex`` binary on PATH.

End-to-end codex verification (real CLI, real API) lives in the
e2e suite when available.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from omnigent.inner import codex_harness
from omnigent.runtime.harnesses import _HARNESS_MODULES


def test_harness_module_registered_in_module_registry() -> None:
    """``"codex"`` resolves to the harness module path.

    Without this entry, the runner subprocess can't find the wrap
    when AP-side tries to spawn it for a ``harness: codex`` spec.
    """
    assert _HARNESS_MODULES.get("codex") == "omnigent.inner.codex_harness"


def test_create_app_returns_fastapi_with_required_routes() -> None:
    """``create_app()`` returns a FastAPI app exposing the harness API.

    Verifies the wrap successfully:
    - Imports the executor adapter + Codex executor.
    - Builds the FastAPI app via ExecutorAdapter.build().
    - Mounts the standard harness routes.

    The actual CodexExecutor is constructed lazily on the first
    turn (not at app build time), so this test passes without a
    real ``codex`` CLI on PATH.
    """
    app = codex_harness.create_app()
    paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
    # Session-keyed harness API: liveness probe + single
    # discriminated-event endpoint per §The Harness API Subset.
    assert "/health" in paths
    assert "/v1/sessions/{conversation_id}/events" in paths


def test_executor_factory_reads_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Factory passes env-var values through to CodexExecutor.

    Locks in the v1 config-flow contract: env vars set in AP's
    process before spawning the subprocess (which inherits
    them) are how the wrap learns its config. Verifies model,
    databricks, profile, cwd, codex_path, web_search,
    disable_native_tools all thread through.
    """
    monkeypatch.setenv("HARNESS_CODEX_MODEL", "test-model-id")
    monkeypatch.setenv("HARNESS_CODEX_GATEWAY", "true")
    monkeypatch.setenv("HARNESS_CODEX_DATABRICKS_PROFILE", "test-profile")
    monkeypatch.setenv("HARNESS_CODEX_GATEWAY_HOST", "https://example.databricks.com")
    monkeypatch.setenv(
        "HARNESS_CODEX_GATEWAY_BASE_URL",
        "https://example.databricks.com/ai-gateway/codex/v1",
    )
    monkeypatch.setenv("HARNESS_CODEX_GATEWAY_AUTH_COMMAND", "printf token")
    monkeypatch.setenv("HARNESS_CODEX_GATEWAY_AUTH_REFRESH_INTERVAL_MS", "900000")
    monkeypatch.setenv("HARNESS_CODEX_CWD", "/tmp/test-cwd")
    monkeypatch.setenv("HARNESS_CODEX_PATH", "/usr/local/bin/codex")
    monkeypatch.setenv("HARNESS_CODEX_ENABLE_WEB_SEARCH", "false")
    monkeypatch.setenv("HARNESS_CODEX_DISABLE_NATIVE_TOOLS", "true")

    captured: dict[str, Any] = {}

    def _fake_init(
        self: Any,
        *,
        cwd: str | None,
        os_env: Any,
        model: str | None,
        codex_path: str | None,
        gateway: bool,
        databricks_profile: str | None,
        gateway_host: str | None,
        base_url_override: str | None,
        gateway_auth_command: str | None,
        gateway_auth_refresh_interval_ms: str | None,
        enable_web_search: bool,
        disable_native_tools: bool,
        **_kwargs: Any,
    ) -> None:
        captured["cwd"] = cwd
        captured["os_env"] = os_env
        captured["model"] = model
        captured["codex_path"] = codex_path
        captured["gateway"] = gateway
        captured["databricks_profile"] = databricks_profile
        captured["gateway_host"] = gateway_host
        captured["base_url_override"] = base_url_override
        captured["gateway_auth_command"] = gateway_auth_command
        captured["gateway_auth_refresh_interval_ms"] = gateway_auth_refresh_interval_ms
        captured["enable_web_search"] = enable_web_search
        captured["disable_native_tools"] = disable_native_tools

    with patch(
        "omnigent.inner.codex_harness.CodexExecutor.__init__",
        _fake_init,
    ):
        codex_harness._build_codex_executor()

    # Each env var threaded through to the corresponding
    # constructor kwarg.
    assert captured["model"] == "test-model-id"
    assert captured["gateway"] is True
    assert captured["databricks_profile"] == "test-profile"
    assert captured["gateway_host"] == "https://example.databricks.com"
    assert captured["base_url_override"] == "https://example.databricks.com/ai-gateway/codex/v1"
    assert captured["gateway_auth_command"] == "printf token"
    assert captured["gateway_auth_refresh_interval_ms"] == "900000"
    assert captured["cwd"] == "/tmp/test-cwd"
    assert captured["codex_path"] == "/usr/local/bin/codex"
    # Inverted defaults verify the truthy parser is consulted
    # for both directions: enable_web_search default is True,
    # we set "false" → expect False; disable_native_tools
    # default is False, we set "true" → expect True.
    assert captured["enable_web_search"] is False
    assert captured["disable_native_tools"] is True
    # Default os_env when no HARNESS_CODEX_OS_ENV is set: the
    # parity-preserving caller_process + sandbox=none.
    os_env_value = captured["os_env"]
    assert os_env_value is not None
    assert os_env_value.type == "caller_process"
    assert os_env_value.sandbox is not None
    assert os_env_value.sandbox.type == "none"


def test_executor_factory_decodes_os_env_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``HARNESS_CODEX_OS_ENV`` decodes into the inner OSEnvSpec.

    Omnigent serializes ``spec.os_env`` via :func:`dataclasses.asdict`
    and JSON-encodes the result; the wrap must reconstruct an
    :class:`OSEnvSpec` (with nested sandbox spec) so
    :class:`CodexExecutor` sees the same config a non-AP mode
    invocation would. Verifies the round-trip on a non-default
    payload — type, cwd, sandbox.type, and a sandbox boolean
    field all flow through.
    """
    import json

    monkeypatch.setenv(
        "HARNESS_CODEX_OS_ENV",
        json.dumps(
            {
                "type": "caller_process",
                "cwd": "/tmp/projected-cwd",
                "sandbox": {
                    "type": "linux_bwrap",
                    "read_paths": ["/srv/data"],
                    "write_paths": None,
                    "write_files": None,
                    "allow_network": False,
                },
                "fork": False,
            }
        ),
    )

    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured["os_env"] = kwargs["os_env"]

    with patch(
        "omnigent.inner.codex_harness.CodexExecutor.__init__",
        _fake_init,
    ):
        codex_harness._build_codex_executor()

    os_env_value = captured["os_env"]
    assert os_env_value is not None
    # The ``cwd`` field carries the spec-author's choice. A
    # regression that dropped it would silently route Codex to
    # the wrong working directory.
    assert os_env_value.cwd == "/tmp/projected-cwd"
    assert os_env_value.sandbox is not None
    assert os_env_value.sandbox.type == "linux_bwrap"
    # ``allow_network=False`` flowed through; a regression that
    # ignored sandbox-specific fields would leave it at the
    # default ``True``.
    assert os_env_value.sandbox.allow_network is False
    assert os_env_value.sandbox.read_paths == ["/srv/data"]


def test_executor_factory_falls_back_on_malformed_os_env_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed ``HARNESS_CODEX_OS_ENV`` falls back to default.

    A malformed payload should NOT crash the wrap — that would
    bring the whole subprocess down on first turn. The wrap
    instead logs a warning and defaults to the parity-preserving
    ``caller_process + sandbox=none`` so the agent still starts.
    """
    monkeypatch.setenv("HARNESS_CODEX_OS_ENV", "{this-is-not-json")
    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured["os_env"] = kwargs["os_env"]

    with patch(
        "omnigent.inner.codex_harness.CodexExecutor.__init__",
        _fake_init,
    ):
        codex_harness._build_codex_executor()

    os_env_value = captured["os_env"]
    assert os_env_value is not None
    assert os_env_value.type == "caller_process"
    assert os_env_value.sandbox is not None
    assert os_env_value.sandbox.type == "none"


@pytest.mark.parametrize(
    "raw_value,expected",
    [
        ("1", True),
        ("true", True),
        ("True", True),
        ("yes", True),
        ("0", False),
        ("false", False),
        ("", False),
        ("anything else", False),
    ],
)
def test_databricks_env_var_truthy_parsing(
    raw_value: str,
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``HARNESS_CODEX_GATEWAY`` parses truthy strings only.

    Mirrors the claude-sdk wrap's parser so operators learn ONE
    set of truthy conventions, not five. The empty string and
    unrecognized values default to False.
    """
    monkeypatch.setenv("HARNESS_CODEX_GATEWAY", raw_value)
    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.codex_harness.CodexExecutor.__init__",
        _fake_init,
    ):
        codex_harness._build_codex_executor()

    assert captured["gateway"] is expected


@pytest.mark.parametrize(
    "raw_value,expected",
    [
        ("1", True),
        ("true", True),
        ("yes", True),
        ("0", False),
        ("false", False),
        # Unset means ENABLED — Codex's web_search is on by
        # default in the legacy non-AP path; the wrap
        # preserves that.
        ("", True),
    ],
)
def test_enable_web_search_default_is_true(
    raw_value: str,
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``HARNESS_CODEX_ENABLE_WEB_SEARCH`` defaults to True when unset.

    Codex's built-in ``web_search`` tool is enabled by default
    in the legacy non-AP path. A regression that flipped
    this default would silently disable web search for every
    AP-driven codex agent.
    """
    if raw_value:
        monkeypatch.setenv("HARNESS_CODEX_ENABLE_WEB_SEARCH", raw_value)
    else:
        monkeypatch.delenv("HARNESS_CODEX_ENABLE_WEB_SEARCH", raising=False)
    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.codex_harness.CodexExecutor.__init__",
        _fake_init,
    ):
        codex_harness._build_codex_executor()

    assert captured["enable_web_search"] is expected


@pytest.mark.parametrize(
    "raw_value,expected",
    [
        ("1", True),
        ("true", True),
        ("0", False),
        ("false", False),
        # Unset means NOT disabled — Codex's native tools stay
        # enabled by default.
        ("", False),
    ],
)
def test_disable_native_tools_default_is_false(
    raw_value: str,
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``HARNESS_CODEX_DISABLE_NATIVE_TOOLS`` defaults to False when unset.

    The legacy non-AP path leaves Codex's native tools on
    by default; the wrap must preserve that.
    """
    if raw_value:
        monkeypatch.setenv("HARNESS_CODEX_DISABLE_NATIVE_TOOLS", raw_value)
    else:
        monkeypatch.delenv("HARNESS_CODEX_DISABLE_NATIVE_TOOLS", raising=False)
    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.codex_harness.CodexExecutor.__init__",
        _fake_init,
    ):
        codex_harness._build_codex_executor()

    assert captured["disable_native_tools"] is expected


@pytest.mark.parametrize(
    "raw_value, expected",
    [
        ('"all"', "all"),
        ('"none"', "none"),
        ('["mlflow-onboarding"]', ["mlflow-onboarding"]),
        ('["a","b","c"]', ["a", "b", "c"]),
    ],
)
def test_skills_filter_env_var_decodes(
    raw_value: str,
    expected: str | list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``HARNESS_CODEX_SKILLS_FILTER`` decodes JSON into ``str`` or
    ``list[str]``.

    Mirrors the claude-sdk wrap's bridge so the spec's
    ``skills:`` field reaches the inner ``CodexExecutor`` at
    construction time. Without this env-var bridge the harness
    wrap falls back to the constructor's ``"all"`` default and
    silently overrides an explicit ``skills: none`` from the
    spec.
    """
    monkeypatch.setenv("HARNESS_CODEX_SKILLS_FILTER", raw_value)
    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.codex_harness.CodexExecutor.__init__",
        _fake_init,
    ):
        codex_harness._build_codex_executor()

    assert captured["skills_filter"] == expected


def test_skills_filter_env_var_missing_falls_back_to_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unset ``HARNESS_CODEX_SKILLS_FILTER`` defaults to ``"all"``."""
    monkeypatch.delenv("HARNESS_CODEX_SKILLS_FILTER", raising=False)
    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.codex_harness.CodexExecutor.__init__",
        _fake_init,
    ):
        codex_harness._build_codex_executor()

    assert captured["skills_filter"] == "all"


def test_bundle_dir_and_agent_name_env_vars_thread_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``HARNESS_CODEX_BUNDLE_DIR`` / ``_AGENT_NAME`` reach the inner
    executor.

    The bundle dir lets the executor source bundled skills from
    ``<bundle>/skills/<name>/`` in addition to host-installed
    ``~/.codex/skills/``.
    """
    from pathlib import Path

    monkeypatch.setenv("HARNESS_CODEX_BUNDLE_DIR", "/tmp/fake/bundle")
    monkeypatch.setenv("HARNESS_CODEX_AGENT_NAME", "my_codex_agent")
    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.codex_harness.CodexExecutor.__init__",
        _fake_init,
    ):
        codex_harness._build_codex_executor()

    assert captured["bundle_dir"] == Path("/tmp/fake/bundle")
    assert captured["agent_name"] == "my_codex_agent"


def test_bundle_dir_unset_passes_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing ``HARNESS_CODEX_BUNDLE_DIR`` resolves to ``None``."""
    monkeypatch.delenv("HARNESS_CODEX_BUNDLE_DIR", raising=False)
    monkeypatch.delenv("HARNESS_CODEX_AGENT_NAME", raising=False)
    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.codex_harness.CodexExecutor.__init__",
        _fake_init,
    ):
        codex_harness._build_codex_executor()

    assert captured["bundle_dir"] is None
    assert captured["agent_name"] is None
