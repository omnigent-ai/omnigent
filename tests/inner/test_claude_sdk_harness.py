"""
Tests for the ``harness: claude-sdk`` wrap shape.

Does NOT exercise the real Claude SDK (no API key needed). Verifies:

- The wrap module exports :func:`create_app` and the registry
  resolves ``"claude-sdk"`` to it.
- ``create_app()`` returns a FastAPI app with the harness API
  subset routes wired up.
- The wrap reads its env-var config at executor construction
  time — verified by inspecting the lazy factory's behavior
  with mocked ``ClaudeSDKExecutor``.

End-to-end claude-sdk verification (real CLI, real API) lives in
the e2e suite and requires API keys.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from omnigent.inner import claude_sdk_harness
from omnigent.runtime.harnesses import _HARNESS_MODULES


def test_harness_module_registered_in_module_registry() -> None:
    """``"claude-sdk"`` resolves to the harness module path.

    Without this entry, the runner subprocess can't find the wrap
    when AP-side tries to spawn it.
    """
    assert _HARNESS_MODULES.get("claude-sdk") == "omnigent.inner.claude_sdk_harness"
    assert _HARNESS_MODULES.get("claude") == "omnigent.inner.claude_sdk_harness"


def test_create_app_returns_fastapi_with_required_routes() -> None:
    """``create_app()`` returns a FastAPI app exposing the harness API.

    Verifies the wrap successfully:
    - Imports the executor adapter + Claude SDK executor.
    - Builds the FastAPI app via ExecutorAdapter.build().
    - Mounts the standard harness routes.

    The actual ClaudeSDKExecutor is constructed lazily on the
    first turn (not at app build time), so this test passes
    without a real ``claude`` CLI on PATH.
    """
    app = claude_sdk_harness.create_app()
    paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
    # Session-keyed harness API: liveness probe + single
    # discriminated-event endpoint per §The Harness API Subset.
    assert "/health" in paths
    assert "/v1/sessions/{conversation_id}/events" in paths


def test_executor_factory_reads_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Factory passes env-var values through to ClaudeSDKExecutor.

    Locks in the v1 config-flow contract: env vars set in AP's
    process before spawning the subprocess (which inherits
    them) are how the wrap learns its config. Verifies model,
    databricks, profile, cwd, permission mode all thread
    through.
    """
    monkeypatch.setenv("HARNESS_CLAUDE_SDK_MODEL", "test-model-id")
    monkeypatch.setenv("HARNESS_CLAUDE_SDK_GATEWAY", "true")
    monkeypatch.setenv("HARNESS_CLAUDE_SDK_DATABRICKS_PROFILE", "test-profile")
    monkeypatch.setenv("HARNESS_CLAUDE_SDK_GATEWAY_HOST", "https://example.databricks.com")
    monkeypatch.setenv(
        "HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL",
        "https://example.databricks.com/ai-gateway/anthropic",
    )
    monkeypatch.setenv("HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND", "printf token")
    monkeypatch.setenv("HARNESS_CLAUDE_SDK_GATEWAY_AUTH_REFRESH_INTERVAL_MS", "900000")
    monkeypatch.setenv("HARNESS_CLAUDE_SDK_CWD", "/tmp/test-cwd")
    monkeypatch.setenv("HARNESS_CLAUDE_SDK_PERMISSION_MODE", "acceptEdits")

    captured: dict[str, Any] = {}

    def _fake_init(
        self: Any,
        *,
        cwd: str | None,
        os_env: Any,
        model: str | None,
        permission_mode: str,
        gateway: bool,
        databricks_profile: str | None,
        gateway_host: str | None,
        base_url_override: str | None,
        gateway_auth_command: str | None,
        gateway_auth_refresh_interval_ms: str | None,
        **_kwargs: Any,
    ) -> None:
        captured["cwd"] = cwd
        captured["os_env"] = os_env
        captured["model"] = model
        captured["permission_mode"] = permission_mode
        captured["gateway"] = gateway
        captured["databricks_profile"] = databricks_profile
        captured["gateway_host"] = gateway_host
        captured["base_url_override"] = base_url_override
        captured["gateway_auth_command"] = gateway_auth_command
        captured["gateway_auth_refresh_interval_ms"] = gateway_auth_refresh_interval_ms

    with patch(
        "omnigent.inner.claude_sdk_harness.ClaudeSDKExecutor.__init__",
        _fake_init,
    ):
        claude_sdk_harness._build_claude_sdk_executor()

    # Each env var threaded through to the corresponding
    # constructor kwarg.
    assert captured["model"] == "test-model-id"
    assert captured["gateway"] is True
    assert captured["databricks_profile"] == "test-profile"
    assert captured["gateway_host"] == "https://example.databricks.com"
    assert captured["base_url_override"] == "https://example.databricks.com/ai-gateway/anthropic"
    assert captured["gateway_auth_command"] == "printf token"
    assert captured["gateway_auth_refresh_interval_ms"] == "900000"
    assert captured["cwd"] == "/tmp/test-cwd"
    assert captured["permission_mode"] == "acceptEdits"
    # When ``HARNESS_CLAUDE_SDK_OS_ENV`` is unset (this test
    # doesn't set it), the wrap defaults to ``caller_process +
    # sandbox=none`` so the SDK exposes its native tools to the
    # LLM. A regression that flipped this back to ``None`` would
    # silently disable Bash/Read/Edit/Write/Glob/Grep — the
    # whole point of step 5g's os_env threading. The check is
    # on the discriminating fields rather than identity so a
    # future tightening (different default sandbox, etc.) is a
    # one-line update.
    os_env_value = captured["os_env"]
    assert os_env_value is not None
    assert os_env_value.type == "caller_process"
    assert os_env_value.sandbox is not None
    assert os_env_value.sandbox.type == "none"


def test_executor_factory_cwd_falls_back_to_runner_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no ``HARNESS_CLAUDE_SDK_CWD``, the factory falls back to the
    runner's ``OMNIGENT_RUNNER_WORKSPACE`` (the folder the user launched
    in, and what the tmux terminal uses) rather than leaving cwd unset —
    which let the SDK root the CLI at the daemon's ``$HOME``. Mirrors the
    kimi / pi / hermes harnesses.
    """
    monkeypatch.delenv("HARNESS_CLAUDE_SDK_CWD", raising=False)
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", "/home/bobby/code/agents")

    captured: dict[str, Any] = {}

    def _fake_init(self: Any, *, cwd: str | None, **_kwargs: Any) -> None:
        captured["cwd"] = cwd

    with patch(
        "omnigent.inner.claude_sdk_harness.ClaudeSDKExecutor.__init__",
        _fake_init,
    ):
        claude_sdk_harness._build_claude_sdk_executor()

    assert captured["cwd"] == "/home/bobby/code/agents"


def test_executor_factory_explicit_cwd_wins_over_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit ``HARNESS_CLAUDE_SDK_CWD`` takes precedence over the
    ``OMNIGENT_RUNNER_WORKSPACE`` fallback."""
    monkeypatch.setenv("HARNESS_CLAUDE_SDK_CWD", "/tmp/explicit")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", "/home/bobby/code/agents")

    captured: dict[str, Any] = {}

    def _fake_init(self: Any, *, cwd: str | None, **_kwargs: Any) -> None:
        captured["cwd"] = cwd

    with patch(
        "omnigent.inner.claude_sdk_harness.ClaudeSDKExecutor.__init__",
        _fake_init,
    ):
        claude_sdk_harness._build_claude_sdk_executor()

    assert captured["cwd"] == "/tmp/explicit"


def test_executor_factory_decodes_os_env_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``HARNESS_CLAUDE_SDK_OS_ENV`` decodes into the inner OSEnvSpec.

    Omnigent serializes ``spec.executor.config["os_env"]`` via
    :func:`dataclasses.asdict` and JSON-encodes the result; the
    wrap must reconstruct an :class:`OSEnvSpec` (with nested
    sandbox spec) so :class:`ClaudeSDKExecutor` sees the same
    config a non-AP mode invocation would. Verifies the round-
    trip on a non-default payload — type, cwd, sandbox.type, and
    a sandbox boolean field all flow through.
    """
    import json

    monkeypatch.setenv(
        "HARNESS_CLAUDE_SDK_OS_ENV",
        json.dumps(
            {
                "type": "caller_process",
                "cwd": "/tmp/projected-cwd",
                "sandbox": {
                    "type": "linux_bwrap",
                    "read_paths": ["/srv/data"],
                    "write_paths": None,
                    "write_files": None,
                    # Non-default to prove every sandbox field
                    # round-trips, not just ``type``.
                    "allow_network": False,
                },
                "fork": False,
            }
        ),
    )

    captured: dict[str, Any] = {}

    def _fake_init(
        self: Any,
        *,
        cwd: str | None,
        os_env: Any,
        model: str | None,
        permission_mode: str,
        gateway: bool,
        databricks_profile: str | None,
        **_kwargs: Any,
    ) -> None:
        captured["os_env"] = os_env

    with patch(
        "omnigent.inner.claude_sdk_harness.ClaudeSDKExecutor.__init__",
        _fake_init,
    ):
        claude_sdk_harness._build_claude_sdk_executor()

    os_env_value = captured["os_env"]
    assert os_env_value is not None
    # The ``cwd`` field carries the spec-author's choice. A
    # regression that dropped it would silently route the SDK
    # to the wrong working directory.
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
    """Malformed ``HARNESS_CLAUDE_SDK_OS_ENV`` falls back to default.

    A malformed payload should NOT crash the wrap — that would
    bring the whole subprocess down on first turn. The wrap
    instead logs a warning and defaults to the parity-preserving
    ``caller_process + sandbox=none`` so the agent still starts
    and the SDK natives stay enabled. Without this fallback a
    bad serialization on AP's side (or an env var hand-tweaked
    by an operator) could silently lobotomize the agent.
    """
    monkeypatch.setenv("HARNESS_CLAUDE_SDK_OS_ENV", "{this-is-not-json")
    captured: dict[str, Any] = {}

    def _fake_init(
        self: Any,
        *,
        cwd: str | None,
        os_env: Any,
        model: str | None,
        permission_mode: str,
        gateway: bool,
        databricks_profile: str | None,
        **_kwargs: Any,
    ) -> None:
        captured["os_env"] = os_env

    with patch(
        "omnigent.inner.claude_sdk_harness.ClaudeSDKExecutor.__init__",
        _fake_init,
    ):
        claude_sdk_harness._build_claude_sdk_executor()

    # Default kicks in: caller_process + sandbox=none. If the
    # wrap raised on bad JSON instead, the test (and the live
    # agent) would never see this assertion — the harness
    # subprocess would have crashed at first turn.
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
    """``HARNESS_CLAUDE_SDK_GATEWAY`` parses truthy strings only.

    The env-var-based contract requires explicit truthy strings
    to enable Databricks routing; anything else (including the
    empty string and unrecognized values) defaults to False.
    Catches a regression where the parser becomes loose and
    mistakenly enables Databricks based on stray env vars.
    """
    monkeypatch.setenv("HARNESS_CLAUDE_SDK_GATEWAY", raw_value)
    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.claude_sdk_harness.ClaudeSDKExecutor.__init__",
        _fake_init,
    ):
        claude_sdk_harness._build_claude_sdk_executor()

    assert captured["gateway"] is expected


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
    """``HARNESS_CLAUDE_SDK_SKILLS_FILTER`` decodes JSON into ``str``
    or ``list[str]``.

    The env-var bridge between the Omnigent runtime and the
    claude-sdk harness subprocess is the load-bearing surface
    for ``skills:`` plumbing — without it the harness wrap
    falls back to the constructor's ``"all"`` default and
    ignores the spec entirely. Verifies all three accepted
    shapes (``"all"``, ``"none"``, list) round-trip.
    """
    monkeypatch.setenv("HARNESS_CLAUDE_SDK_SKILLS_FILTER", raw_value)
    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.claude_sdk_harness.ClaudeSDKExecutor.__init__",
        _fake_init,
    ):
        claude_sdk_harness._build_claude_sdk_executor()

    assert captured["skills_filter"] == expected


def test_skills_filter_env_var_missing_falls_back_to_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unset ``HARNESS_CLAUDE_SDK_SKILLS_FILTER`` defaults to ``"all"``.

    Matches the SDK's ``skills="all"`` default so legacy
    deployments that don't yet ship the env var keep their
    existing host-skill discovery behavior.
    """
    monkeypatch.delenv("HARNESS_CLAUDE_SDK_SKILLS_FILTER", raising=False)
    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.claude_sdk_harness.ClaudeSDKExecutor.__init__",
        _fake_init,
    ):
        claude_sdk_harness._build_claude_sdk_executor()

    assert captured["skills_filter"] == "all"


def test_skills_filter_env_var_malformed_json_falls_back_to_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed JSON in ``HARNESS_CLAUDE_SDK_SKILLS_FILTER`` defaults
    to ``"all"`` rather than crashing the harness boot.

    A bad serialization shouldn't take the whole subprocess
    down on first turn — the wrap logs and degrades to the
    SDK's default skill-discovery behavior.
    """
    monkeypatch.setenv("HARNESS_CLAUDE_SDK_SKILLS_FILTER", "{not-json")
    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.claude_sdk_harness.ClaudeSDKExecutor.__init__",
        _fake_init,
    ):
        claude_sdk_harness._build_claude_sdk_executor()

    assert captured["skills_filter"] == "all"


def test_bundle_dir_and_agent_name_env_vars_thread_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``HARNESS_CLAUDE_SDK_BUNDLE_DIR`` / ``_AGENT_NAME`` reach the
    inner executor.

    Together they wire the SDK ``--plugin-dir`` so any
    ``<bundle>/skills/<name>/SKILL.md`` files in the agent's
    bundle get exposed as bundled skills, with a stable
    ``<agent>:<skill>`` plugin namespace via the
    ``.claude-plugin/plugin.json`` manifest the inner executor
    writes at construction time.
    """
    from pathlib import Path

    monkeypatch.setenv("HARNESS_CLAUDE_SDK_BUNDLE_DIR", "/tmp/fake/bundle")
    monkeypatch.setenv("HARNESS_CLAUDE_SDK_AGENT_NAME", "hello_world")
    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.claude_sdk_harness.ClaudeSDKExecutor.__init__",
        _fake_init,
    ):
        claude_sdk_harness._build_claude_sdk_executor()

    assert captured["bundle_dir"] == Path("/tmp/fake/bundle")
    assert captured["agent_name"] == "hello_world"


def test_bundle_dir_unset_passes_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing ``HARNESS_CLAUDE_SDK_BUNDLE_DIR`` resolves to ``None``.

    Catches a regression where the wrap silently coerces
    ``""`` to ``Path("")``: a bogus path that would break the
    SDK's ``--plugin-dir`` wiring rather than skipping it.
    """
    monkeypatch.delenv("HARNESS_CLAUDE_SDK_BUNDLE_DIR", raising=False)
    monkeypatch.delenv("HARNESS_CLAUDE_SDK_AGENT_NAME", raising=False)
    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.claude_sdk_harness.ClaudeSDKExecutor.__init__",
        _fake_init,
    ):
        claude_sdk_harness._build_claude_sdk_executor()

    assert captured["bundle_dir"] is None
    assert captured["agent_name"] is None
