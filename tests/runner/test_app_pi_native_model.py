"""Tests for pi-native model resolution from the agent spec.

``_pi_native_model_from_spec`` is the seam that turns a session's
``executor.model`` (set via a config.yaml ``model:`` key) into the model
threaded into ``resolve_pi_native_provider(model=...)`` — which renders it
into the runner-owned Pi ``models.json`` (and the appended ``--model``).

Unlike cursor-native, a gateway-routed id (``databricks-*``) is KEPT: the
runner-owned Pi process routes through the Databricks AI Gateway, whose
``models.json`` selects the model by its gateway id.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.entities.session_resources import SessionResourceView
from omnigent.runner.app import (
    ResolvedSpec,
    _auto_create_pi_terminal,
    _pi_native_model_from_spec,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec


def _spec(model: str | None) -> AgentSpec:
    """Build a minimal agent spec carrying *model* on its executor block."""
    return AgentSpec(spec_version=1, name="pi", executor=ExecutorSpec(model=model))


def test_pi_native_model_passthrough() -> None:
    """A pinned model id is returned verbatim."""
    assert _pi_native_model_from_spec(_spec("databricks-claude-opus-4-7")) == (
        "databricks-claude-opus-4-7"
    )


def test_pi_native_model_keeps_gateway_id() -> None:
    """Gateway-routed ids are usable here (Pi routes through the gateway)."""
    assert _pi_native_model_from_spec(_spec("databricks-claude-sonnet-4-6")) == (
        "databricks-claude-sonnet-4-6"
    )
    assert _pi_native_model_from_spec(_spec("openai/gpt-4o")) == "openai/gpt-4o"


def test_pi_native_model_no_pin_returns_none() -> None:
    """No model declared → None (Pi keeps the provider's default model)."""
    assert _pi_native_model_from_spec(_spec(None)) is None
    assert _pi_native_model_from_spec(_spec("")) is None


def test_pi_native_model_none_spec() -> None:
    """A missing spec yields no model override."""
    assert _pi_native_model_from_spec(None) is None


def test_pi_native_model_from_resolved_spec_wrapper() -> None:
    """The model is read through a ``ResolvedSpec`` wrapper too."""
    wrapped = ResolvedSpec(spec=_spec("databricks-claude-opus-4-7"), workdir=Path("/tmp"))
    assert _pi_native_model_from_spec(wrapped) == "databricks-claude-opus-4-7"


def _key_provider_config() -> dict[str, Any]:
    """A key-kind anthropic provider config (Pi's native surface)."""
    return {
        "providers": {
            "anthropic": {
                "kind": "key",
                "default": True,
                "anthropic": {
                    "base_url": "https://api.anthropic.com",
                    "api_key": "sk-test-literal",
                    "models": {"default": "claude-sonnet-4-6"},
                },
            }
        }
    }


@pytest.mark.asyncio
async def test_auto_create_pi_terminal_threads_spec_model_into_models_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: the spec's ``executor.model`` reaches the generated models.json.

    Drives ``_auto_create_pi_terminal`` with a spec pinning
    ``claude-opus-4-7`` and a key-kind provider whose family default is
    ``claude-sonnet-4-6``. The threaded override must win: the generated
    ``models.json`` selects ``claude-opus-4-7`` and the appended Pi
    ``--model`` arg reflects it. This is the runner-side seam the feature
    adds — without threading the spec model, the models.json would carry
    the family default instead.

    :param tmp_path: Temp dir backing the pi-native bridge root.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    import omnigent.pi_native_bridge as pi_bridge
    import omnigent.pi_native_credentials as creds

    session_id = "conv_pi_model_e2e"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # Redirect the bridge tree into tmp so the generated managed Pi config dir
    # (and its models.json) lands somewhere isolated and inspectable.
    monkeypatch.setattr(pi_bridge, "_BRIDGE_ROOT", tmp_path / "pi-native")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(workspace))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)
    # Resolve a Pi executable without requiring the real binary on PATH.
    monkeypatch.setattr("omnigent.pi_native.resolve_pi_executable", lambda: "/usr/bin/pi")

    # ``resolve_pi_native_provider``'s default config_loader is bound at def
    # time, so inject the test config by patching the module symbol the runner
    # imports locally — recording the ``model`` kwarg it is called with.
    real_resolve = creds.resolve_pi_native_provider
    captured: dict[str, Any] = {}

    def _resolve_with_test_config(*, model: str | None = None, config_loader: Any = None):
        captured["model"] = model
        return real_resolve(model=model, config_loader=_key_provider_config)

    monkeypatch.setattr(creds, "resolve_pi_native_provider", _resolve_with_test_config)

    class _SnapshotClient:
        """Fresh pi-native session snapshot (no launch args / external id)."""

        async def get(self, url: str, *, timeout: float) -> httpx.Response:
            del url, timeout
            return httpx.Response(
                200,
                json={
                    "workspace": str(workspace),
                    "terminal_launch_args": None,
                    "external_session_id": None,
                },
                request=httpx.Request("GET", f"/v1/sessions/{session_id}"),
            )

    launched: dict[str, Any] = {}

    class _FakeResourceRegistry:
        """Captures the launched terminal spec (args + env)."""

        terminal_registry = None

        async def launch_required_terminal(
            self,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            *,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            del terminal_name, session_key, resource_role, parent_os_env
            launched["args"] = list(spec.args)
            launched["env"] = dict(spec.env)
            return SessionResourceView(
                id="terminal_pi_main",
                type="terminal",
                session_id=session_id,
                name="pi",
            )

    spec = AgentSpec(
        spec_version=1,
        name="pi-model-e2e",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "pi-native"},
            model="claude-opus-4-7",
        ),
    )

    await _auto_create_pi_terminal(
        session_id,
        _FakeResourceRegistry(),  # type: ignore[arg-type]
        lambda _sid, _event: None,
        server_client=_SnapshotClient(),  # type: ignore[arg-type]
        agent_spec=spec,
    )

    # The runner threaded the spec model into resolve_pi_native_provider.
    assert captured["model"] == "claude-opus-4-7"

    # The appended Pi args select the override, not the family default.
    args = launched["args"]
    assert "--model" in args
    assert args[args.index("--model") + 1] == "claude-opus-4-7"
    assert "--provider" in args

    # The managed config dir env was set and its models.json selects the override.
    agent_dir = Path(launched["env"]["PI_CODING_AGENT_DIR"])
    models = json.loads((agent_dir / "models.json").read_text(encoding="utf-8"))
    assert models["providers"]["omnigent"]["models"] == [{"id": "claude-opus-4-7"}]


@pytest.mark.asyncio
async def test_auto_create_pi_terminal_no_spec_model_uses_provider_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no spec model, the provider's family default is used (unchanged).

    Guards the ``None`` case: a spec without ``executor.model`` must leave
    Pi on the provider default (``claude-sonnet-4-6`` here), not break the
    launch.

    :param tmp_path: Temp dir backing the pi-native bridge root.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    import omnigent.pi_native_bridge as pi_bridge
    import omnigent.pi_native_credentials as creds

    session_id = "conv_pi_model_default"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(pi_bridge, "_BRIDGE_ROOT", tmp_path / "pi-native")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(workspace))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)
    monkeypatch.setattr("omnigent.pi_native.resolve_pi_executable", lambda: "/usr/bin/pi")

    real_resolve = creds.resolve_pi_native_provider
    captured: dict[str, Any] = {}

    def _resolve_with_test_config(*, model: str | None = None, config_loader: Any = None):
        captured["model"] = model
        return real_resolve(model=model, config_loader=_key_provider_config)

    monkeypatch.setattr(creds, "resolve_pi_native_provider", _resolve_with_test_config)

    class _SnapshotClient:
        async def get(self, url: str, *, timeout: float) -> httpx.Response:
            del url, timeout
            return httpx.Response(
                200,
                json={
                    "workspace": str(workspace),
                    "terminal_launch_args": None,
                    "external_session_id": None,
                },
                request=httpx.Request("GET", f"/v1/sessions/{session_id}"),
            )

    launched: dict[str, Any] = {}

    class _FakeResourceRegistry:
        terminal_registry = None

        async def launch_required_terminal(
            self,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            *,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            del terminal_name, session_key, resource_role, parent_os_env
            launched["args"] = list(spec.args)
            launched["env"] = dict(spec.env)
            return SessionResourceView(
                id="terminal_pi_main",
                type="terminal",
                session_id=session_id,
                name="pi",
            )

    spec = AgentSpec(
        spec_version=1,
        name="pi-default",
        executor=ExecutorSpec(type="omnigent", config={"harness": "pi-native"}),
    )

    await _auto_create_pi_terminal(
        session_id,
        _FakeResourceRegistry(),  # type: ignore[arg-type]
        lambda _sid, _event: None,
        server_client=_SnapshotClient(),  # type: ignore[arg-type]
        agent_spec=spec,
    )

    assert captured["model"] is None
    agent_dir = Path(launched["env"]["PI_CODING_AGENT_DIR"])
    models = json.loads((agent_dir / "models.json").read_text(encoding="utf-8"))
    assert models["providers"]["omnigent"]["models"] == [{"id": "claude-sonnet-4-6"}]
