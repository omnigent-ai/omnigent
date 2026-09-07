"""
Tests for the ``harness: omp`` wrap shape.

Mirror of ``tests/inner/test_codex_harness.py`` and
``tests/inner/test_claude_sdk_harness.py`` — verifies the wrap
module has the same shape (registry entry, FastAPI app routes,
env-var-driven lazy executor construction). Does NOT exercise
the real omp CLI; the inner ``OmpExecutor.__init__`` is mocked so
the tests pass without a ``omp`` binary on PATH.

End-to-end pi verification (real CLI, real API) lives in the
e2e suite, gated on the binary being available.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from omnigent.inner import omp_harness
from omnigent.runtime.harnesses import _HARNESS_MODULES


def test_harness_module_registered_in_module_registry() -> None:
    """``"omp"`` resolves to the harness module path.

    Without this entry, the runner subprocess can't find the wrap
    when AP-side tries to spawn it for a ``harness: omp`` spec.
    """
    assert _HARNESS_MODULES.get("omp") == "omnigent.inner.omp_harness"


def test_create_app_returns_fastapi_with_required_routes() -> None:
    """``create_app()`` returns a FastAPI app exposing the harness API.

    Verifies the wrap successfully:
    - Imports the executor adapter + omp executor module.
    - Builds the FastAPI app via ExecutorAdapter.build().
    - Mounts the standard harness routes.

    The actual OmpExecutor is constructed lazily on the first
    turn (not at app build time), so this test passes without
    a real ``omp`` CLI on PATH.
    """
    app = omp_harness.create_app()
    paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
    # Session-keyed harness API: liveness probe + single
    # discriminated-event endpoint per §The Harness API Subset.
    assert "/health" in paths
    assert "/v1/sessions/{conversation_id}/events" in paths


def test_executor_factory_reads_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Factory passes env-var values through to OmpExecutor.

    Locks in the v1 config-flow contract: env vars set in AP's
    process before spawning the subprocess (which inherits
    them) are how the wrap learns its config. Verifies model,
    databricks, profile, cwd, omp_path all thread through.
    """
    monkeypatch.setenv("HARNESS_OMP_MODEL", "test-model-id")
    monkeypatch.setenv("HARNESS_OMP_GATEWAY", "true")
    monkeypatch.setenv("HARNESS_OMP_DATABRICKS_PROFILE", "test-profile")
    monkeypatch.setenv("HARNESS_OMP_GATEWAY_HOST", "https://example.databricks.com")
    monkeypatch.setenv(
        "HARNESS_OMP_GATEWAY_BASE_URL",
        "https://example.databricks.com/ai-gateway/anthropic",
    )
    monkeypatch.setenv(
        "HARNESS_OMP_GATEWAY_BASE_URLS",
        json.dumps(
            {
                "claude": "https://example.databricks.com/ai-gateway/anthropic",
                "openai": "https://example.databricks.com/ai-gateway/codex/v1",
            }
        ),
    )
    monkeypatch.setenv("HARNESS_OMP_GATEWAY_OPENAI_WIRE_API", "responses")
    monkeypatch.setenv("HARNESS_OMP_GATEWAY_AUTH_COMMAND", "printf token")
    monkeypatch.setenv("HARNESS_OMP_CWD", "/tmp/test-cwd")
    # Unlike pi there is no legacy ``HARNESS_OMP_PATH`` alias: a stale
    # deprecated-style var must NOT resolve — only ``OMNIGENT_OMP_PATH``
    # (deleted here) or PATH discovery.
    monkeypatch.setenv("HARNESS_OMP_PATH", "/usr/local/bin/omp")
    monkeypatch.delenv("OMNIGENT_OMP_PATH", raising=False)

    captured: dict[str, Any] = {}

    def _fake_init(
        self: Any,
        *,
        cwd: str | None,
        os_env: Any,
        model: str | None,
        omp_path: str | None,
        gateway: bool,
        databricks_profile: str | None,
        gateway_host: str | None,
        base_url_override: str | None,
        base_urls_override: dict[str, str] | None,
        openai_wire_api: str | None,
        gateway_auth_command: str | None,
        **_kwargs: Any,
    ) -> None:
        captured["cwd"] = cwd
        captured["os_env"] = os_env
        captured["model"] = model
        captured["omp_path"] = omp_path
        captured["gateway"] = gateway
        captured["databricks_profile"] = databricks_profile
        captured["gateway_host"] = gateway_host
        captured["base_url_override"] = base_url_override
        captured["base_urls_override"] = base_urls_override
        captured["openai_wire_api"] = openai_wire_api
        captured["gateway_auth_command"] = gateway_auth_command

    with patch(
        "omnigent.inner.omp_harness.OmpExecutor.__init__",
        _fake_init,
    ):
        omp_harness._build_omp_executor()

    # Each env var threaded through to the corresponding
    # constructor kwarg.
    assert captured["model"] == "test-model-id"
    assert captured["gateway"] is True
    assert captured["databricks_profile"] == "test-profile"
    assert captured["gateway_host"] == "https://example.databricks.com"
    assert captured["base_url_override"] == "https://example.databricks.com/ai-gateway/anthropic"
    assert captured["base_urls_override"] == {
        "claude": "https://example.databricks.com/ai-gateway/anthropic",
        "openai": "https://example.databricks.com/ai-gateway/codex/v1",
    }
    assert captured["openai_wire_api"] == "responses"
    assert captured["gateway_auth_command"] == "printf token"
    assert captured["cwd"] == "/tmp/test-cwd"
    assert captured["omp_path"] is None
    # Default os_env when no HARNESS_OMP_OS_ENV is set: the
    # parity-preserving caller_process + sandbox=none.
    os_env_value = captured["os_env"]
    assert os_env_value is not None
    assert os_env_value.type == "caller_process"
    assert os_env_value.sandbox is not None
    assert os_env_value.sandbox.type == "none"


def test_executor_factory_uses_omnigient_omp_path_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``OMNIGENT_OMP_PATH`` resolves as the omp CLI override."""
    monkeypatch.setenv("OMNIGENT_OMP_PATH", "/custom/omp")
    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.omp_harness.OmpExecutor.__init__",
        _fake_init,
    ):
        omp_harness._build_omp_executor()

    assert captured["omp_path"] == "/custom/omp"


def test_executor_factory_decodes_os_env_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``HARNESS_OMP_OS_ENV`` decodes into the inner OSEnvSpec.

    Omnigent serializes ``spec.os_env`` via :func:`dataclasses.asdict`
    and JSON-encodes the result; the wrap must reconstruct an
    :class:`OSEnvSpec` (with nested sandbox spec) so
    :class:`OmpExecutor` sees the same config a non-AP mode
    invocation would.
    """
    import json

    monkeypatch.setenv(
        "HARNESS_OMP_OS_ENV",
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
        "omnigent.inner.omp_harness.OmpExecutor.__init__",
        _fake_init,
    ):
        omp_harness._build_omp_executor()

    os_env_value = captured["os_env"]
    assert os_env_value is not None
    assert os_env_value.cwd == "/tmp/projected-cwd"
    assert os_env_value.sandbox is not None
    assert os_env_value.sandbox.type == "linux_bwrap"
    assert os_env_value.sandbox.allow_network is False
    assert os_env_value.sandbox.read_paths == ["/srv/data"]


def test_executor_factory_falls_back_on_malformed_os_env_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed ``HARNESS_OMP_OS_ENV`` falls back to default.

    A malformed payload should NOT crash the wrap — that would
    bring the whole subprocess down on first turn. The wrap
    instead logs a warning and defaults to the parity-preserving
    ``caller_process + sandbox=none``.
    """
    monkeypatch.setenv("HARNESS_OMP_OS_ENV", "{this-is-not-json")
    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured["os_env"] = kwargs["os_env"]

    with patch(
        "omnigent.inner.omp_harness.OmpExecutor.__init__",
        _fake_init,
    ):
        omp_harness._build_omp_executor()

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
    """``HARNESS_OMP_GATEWAY`` parses truthy strings only.

    Mirrors the claude-sdk and codex wraps' parsers so operators
    learn ONE set of truthy conventions, not five.
    """
    monkeypatch.setenv("HARNESS_OMP_GATEWAY", raw_value)
    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.omp_harness.OmpExecutor.__init__",
        _fake_init,
    ):
        omp_harness._build_omp_executor()

    assert captured["gateway"] is expected


@pytest.mark.parametrize(
    "raw_value, expected",
    [
        ('"all"', "all"),
        ('"none"', "none"),
        ('["alpha"]', ["alpha"]),
        ('["alpha","beta","gamma"]', ["alpha", "beta", "gamma"]),
    ],
)
def test_skills_filter_env_var_decodes(
    raw_value: str,
    expected: str | list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``HARNESS_OMP_SKILLS_FILTER`` decodes JSON into ``str`` or
    ``list[str]``.

    Mirrors the claude-sdk and codex env-var bridges. Without
    this bridge the harness wrap falls back to the constructor's
    ``"all"`` default and silently overrides explicit
    ``skills: none`` from the spec — same regression that
    prompted the original bridge work.
    """
    monkeypatch.setenv("HARNESS_OMP_SKILLS_FILTER", raw_value)
    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.omp_harness.OmpExecutor.__init__",
        _fake_init,
    ):
        omp_harness._build_omp_executor()

    assert captured["skills_filter"] == expected


def test_skills_filter_env_var_missing_falls_back_to_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing ``HARNESS_OMP_SKILLS_FILTER`` defaults to ``"all"``."""
    monkeypatch.delenv("HARNESS_OMP_SKILLS_FILTER", raising=False)
    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.omp_harness.OmpExecutor.__init__",
        _fake_init,
    ):
        omp_harness._build_omp_executor()

    assert captured["skills_filter"] == "all"


def test_bundle_dir_and_agent_name_env_vars_thread_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``HARNESS_OMP_BUNDLE_DIR`` / ``_AGENT_NAME`` reach the inner
    executor.

    Bundle dir gates the omp resolver's bundle source — without it
    the agent-shipped skills are invisible to the executor, so
    ``"all"`` and named-list cases would silently drop them.
    """
    from pathlib import Path

    monkeypatch.setenv("HARNESS_OMP_BUNDLE_DIR", "/tmp/fake/pi/bundle")
    monkeypatch.setenv("HARNESS_OMP_AGENT_NAME", "my_omp_agent")
    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.omp_harness.OmpExecutor.__init__",
        _fake_init,
    ):
        omp_harness._build_omp_executor()

    assert captured["bundle_dir"] == Path("/tmp/fake/pi/bundle")
    assert captured["agent_name"] == "my_omp_agent"


def test_bundle_dir_unset_passes_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing ``HARNESS_OMP_BUNDLE_DIR`` resolves to ``None``,
    not ``Path("")`` — a bogus path would crash the resolver."""
    monkeypatch.delenv("HARNESS_OMP_BUNDLE_DIR", raising=False)
    monkeypatch.delenv("HARNESS_OMP_AGENT_NAME", raising=False)
    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.omp_harness.OmpExecutor.__init__",
        _fake_init,
    ):
        omp_harness._build_omp_executor()

    assert captured["bundle_dir"] is None
    assert captured["agent_name"] is None


@pytest.mark.parametrize(
    "raw_value, expected",
    [
        ("", None),
        ("{not-json", None),
        ("[1, 2]", None),
        ('"just-a-string"', None),
        ('{"claude": 123}', {}),
        (
            '{"claude": "https://a.example.com", "openai": 42}',
            {"claude": "https://a.example.com"},
        ),
    ],
)
def test_gateway_base_urls_rejects_bad_shapes(
    raw_value: str,
    expected: dict[str, str] | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed ``HARNESS_OMP_GATEWAY_BASE_URLS`` degrades, never crashes.

    Non-object JSON and non-string URLs cannot drive the models.yml writer,
    so they resolve to ``None`` (unset) or drop the bad entries — the
    executor then falls back to workspace-derived defaults.
    """
    if raw_value:
        monkeypatch.setenv("HARNESS_OMP_GATEWAY_BASE_URLS", raw_value)
    else:
        monkeypatch.delenv("HARNESS_OMP_GATEWAY_BASE_URLS", raising=False)
    assert omp_harness._resolve_gateway_base_urls() == expected
