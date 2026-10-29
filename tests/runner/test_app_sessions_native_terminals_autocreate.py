"""Tests for native terminal auto-creation across supported harnesses."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from omnigent.entities.session_resources import SessionResourceView
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harnesses.antigravity_native.bridge import (
    ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY,
    AntigravityNativeBridgeState,
)
from omnigent.harnesses.antigravity_native.bridge import (
    prepare_bridge_dir as prepare_antigravity_bridge_dir,
)
from omnigent.harnesses.antigravity_native.bridge import (
    write_bridge_state as write_antigravity_bridge_state,
)
from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.harnesses.claude_native.bridge import (
    BRIDGE_ID_LABEL_KEY,
    ClaudeNativeHookInterpreterMismatchError,
    bridge_dir_for_bridge_id,
    prepare_bridge_dir,
    read_permission_hook_config,
)
from omnigent.harnesses.codex_native.bridge import (
    CODEX_NATIVE_BRIDGE_ID_LABEL_KEY,
    CodexNativeBridgeState,
)
from omnigent.harnesses.codex_native.bridge import (
    prepare_bridge_dir as prepare_codex_bridge_dir,
)
from omnigent.harnesses.codex_native.bridge import (
    write_bridge_state as write_codex_bridge_state,
)
from omnigent.harnesses.cursor_native import bridge as cursor_native_bridge
from omnigent.harnesses.kiro_native import bridge as kiro_native_bridge
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.terminal import TerminalInstance
from omnigent.runner import create_runner_app, subagent_work
from omnigent.runner.app import (
    ResolvedSpec,
    _agent_os_env_from_spec,
    _auto_create_claude_terminal,
    _auto_create_cursor_terminal,
    _auto_create_kiro_terminal,
    _auto_create_pi_terminal,
    _codex_native_launch_config,
    _KiroNativeLaunchConfig,
    _load_claude_launch_metadata,
    _log_terminal_lookup_miss,
    _PiNativeLaunchConfig,
    _publish_native_terminal_start_error,
    _publish_terminal_pending,
    _terminal_lookup_miss_log_state,
)
from omnigent.runner.native.orchestration import _build_pi_native_args
from omnigent.runner.resource_registry import (
    CLAUDE_NATIVE_TERMINAL_ROLE,
    KIRO_NATIVE_TERMINAL_ROLE,
    PI_NATIVE_TERMINAL_ROLE,
    SessionResourceRegistry,
)
from omnigent.runner.session_init_protocol import RunnerSessionInitEnvelope
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.stores.conversation_store import FORK_CARRY_HISTORY_LABEL_KEY
from omnigent.terminals import TerminalRegistry
from tests._helpers.pi_native import seed_pi_own_login_catalog
from tests._helpers.providers import isolate_provider_resolution
from tests.budgets import budget
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
)
from tests.runner.helpers import NullServerClient, make_test_terminal_instance

_PI_SESSION_ID = "47f049b9d13df4db397c7f46859b825f"


async def _no_op_forwarder(**kwargs: Any) -> None:
    del kwargs


class _RecordingClaudeRegistry:
    """Captures the launched terminal spec."""

    terminal_registry = None

    def __init__(self, captured: dict[str, Any]) -> None:
        self.captured = captured

    async def launch_required_terminal(
        self,
        *,
        session_id: str,
        terminal_name: str,
        session_key: str,
        spec: Any,
        resource_role: str | None = None,
        parent_os_env: Any = None,
    ) -> SessionResourceView:
        """Record the spec and return a terminal resource view."""
        del terminal_name, session_key
        self.captured["spec"] = spec
        return SessionResourceView(
            id="terminal_claude_main",
            type="terminal",
            session_id=session_id,
            name="claude:main",
            metadata={"terminal_name": "claude", "session_key": "main", "running": True},
        )


class _ResumeSnapshotServerClient(NullServerClient):
    """Server client serving one session snapshot, empty labels and optional items."""

    def __init__(
        self,
        session_url: str,
        snapshot: dict[str, Any],
        items: list[dict[str, Any]] | None = None,
    ) -> None:
        self._session_url = session_url
        self._snapshot = snapshot
        self._items = items

    async def get(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        """Return empty labels (so the bridge id is the session id), items or the snapshot."""
        del kwargs
        if url.endswith("/labels"):
            payload: dict[str, Any] = {"labels": {}}
        elif self._items is not None and url == f"{self._session_url}/items":
            payload = {"data": self._items}
        else:
            assert url == self._session_url
            payload = self._snapshot

        class _Response(NullServerClient._Response):
            """Canned JSON response."""

            def json(self) -> dict[str, Any]:
                """Return the canned payload."""
                return payload

        return _Response()


def _load_claude_invocation_settings(args: list[str]) -> dict[str, Any]:
    settings_path = Path(args[args.index("--settings") + 1])
    return json.loads(settings_path.read_text(encoding="utf-8"))


def test_read_relay_policy_config_returns_coords_from_tool_relay_json(
    tmp_path: Path,
) -> None:
    """read_relay_policy_config extracts relay URL, token, and session_id."""
    from omnigent.native.native_policy_hook import read_relay_policy_config

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    relay_file = bridge_dir / "tool_relay.json"
    relay_file.write_text(
        '{"url": "http://127.0.0.1:9999", "token": "tok123", "session_id": "conv_abc"}'
    )
    result = read_relay_policy_config(bridge_dir)
    assert result == ("http://127.0.0.1:9999", "tok123", "conv_abc")


def test_read_relay_policy_config_returns_none_when_missing(tmp_path: Path) -> None:
    """read_relay_policy_config returns None when tool_relay.json absent."""
    from omnigent.native.native_policy_hook import read_relay_policy_config

    assert read_relay_policy_config(tmp_path) is None


def test_read_relay_policy_config_returns_none_when_session_id_absent(
    tmp_path: Path,
) -> None:
    """read_relay_policy_config returns None when session_id absent (relay not policy-capable)."""
    from omnigent.native.native_policy_hook import read_relay_policy_config

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    relay_file = bridge_dir / "tool_relay.json"
    relay_file.write_text('{"url": "http://127.0.0.1:9999", "token": "tok123"}')
    assert read_relay_policy_config(bridge_dir) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("instruction_spec", "env_denylist", "expected_env_unset"),
    [
        ("missing", None, []),
        (
            "missing",
            " OPENAI_API_KEY,ANTHROPIC_AUTH_TOKEN,OPENAI_API_KEY, ",
            ["ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"],
        ),
        ("empty", None, []),
        ("raw", None, []),
        ("resolved", None, []),
        ("resolved-replace", None, []),
    ],
)
async def test_auto_create_pi_terminal_launches_required_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env_denylist: str | None,
    expected_env_unset: list[str],
    instruction_spec: str,
) -> None:
    """Launch a required terminal with session-local authored instructions.

    The registry exposes only the required-terminal API; provider discovery
    must leave the event loop responsive and preserve the operator denylist.
    """
    import omnigent.harnesses.pi_native.bridge as pi_native_bridge
    import omnigent.harnesses.pi_native.credentials as pi_native_credentials
    import omnigent.harnesses.pi_native.main as pi_native
    import omnigent.runner.app as runner_app_mod

    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    if env_denylist is None:
        monkeypatch.delenv("OMNIGENT_PI_ENV_UNSET", raising=False)
    else:
        monkeypatch.setenv("OMNIGENT_PI_ENV_UNSET", env_denylist)
    monkeypatch.setattr(pi_native_bridge, "_BRIDGE_ROOT", tmp_path / "pi-bridge")
    # The lifecycle of the launch — not the binary or credentials — is under
    # test, so neither a real Pi install nor a configured provider is needed.
    monkeypatch.setattr(pi_native, "resolve_pi_executable", lambda: "pi")
    loop = asyncio.get_running_loop()
    provider_waits: list[bool] = []

    def _slow_provider(**_kwargs: Any) -> None:
        # Credential discovery must let the runner service its stream heartbeat.
        serviced = threading.Event()
        loop.call_soon_threadsafe(serviced.set)
        provider_waits.append(serviced.wait(timeout=2))

    monkeypatch.setattr(pi_native_credentials, "resolve_pi_native_provider", _slow_provider)

    # Skip the GET /v1/sessions round-trip: hand the flow a ready launch
    # config pointing at the tmp workspace.
    async def _fake_launch_config(**_kwargs: Any) -> _PiNativeLaunchConfig:
        return _PiNativeLaunchConfig(
            workspace=tmp_path,
            server_url="http://127.0.0.1:8000",
            terminal_launch_args=["--append-system-prompt", "User addition"],
            external_session_id=None,
        )

    monkeypatch.setattr("omnigent.runner.app._pi_native_launch_config", _fake_launch_config)
    # TUI-typed turns skip the executor's refresh, so the runner owns one per launch.
    supervised: list[tuple[Path, str]] = []

    async def _supervise(bridge_dir: Path, url: str) -> None:
        supervised.append((bridge_dir, url))

    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: lambda: "tok")
    monkeypatch.setattr(
        "omnigent.harnesses.pi_native.auth_refresh.supervise_pi_auth_refresh", _supervise
    )

    captured: dict[str, Any] = {}

    class _FakeResourceRegistry:
        """Omit launch_terminal so regressions to the optional-terminal API fail."""

        terminal_registry = None

        async def launch_required_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """Record the launch and return a terminal resource view."""
            captured["terminal_name"] = terminal_name
            captured["session_key"] = session_key
            captured["resource_role"] = resource_role
            captured["spec"] = spec
            return SessionResourceView(
                id="terminal_pi_main",
                type="terminal",
                session_id=session_id,
                name="pi:main",
                metadata={"terminal_name": "pi", "session_key": "main", "running": True},
            )

    published: list[dict[str, Any]] = []

    instructions = "  Follow the authored role. π\nKeep this whitespace.  "
    agent_spec = None
    if instruction_spec != "missing":
        agent_spec = AgentSpec(
            spec_version=1,
            name="pi",
            executor=ExecutorSpec(),
            instructions=instructions if instruction_spec != "empty" else "   ",
        )
        if instruction_spec == "resolved-replace":
            agent_spec.executor.config.update(context_files=False, system_prompt_mode="replace")
        if instruction_spec.startswith("resolved"):
            agent_spec = ResolvedSpec(spec=agent_spec, workdir=tmp_path)

    bridge_dir = pi_native_bridge.prepare_bridge_dir(_PI_SESSION_ID)
    instructions_path = bridge_dir / "agent_instructions.md"
    instructions_path.write_text("Stale role")

    await _auto_create_pi_terminal(
        _PI_SESSION_ID,
        _FakeResourceRegistry(),  # type: ignore[arg-type]
        lambda _sid, evt: published.append(evt),
        server_client=NullServerClient(),  # type: ignore[arg-type]
        agent_spec=agent_spec,
    )

    # Required lifecycle (parity with claude-native), correct terminal identity.
    assert captured["terminal_name"] == "pi"
    assert captured["session_key"] == "main"
    assert captured["resource_role"] == PI_NATIVE_TERMINAL_ROLE
    assert captured["spec"].command == "pi"
    # Preserve the exited pane so the idle watcher can report its output.
    assert captured["spec"].keep_alive_after_exit is True
    assert captured["spec"].env_unset == expected_env_unset
    assert provider_waits == [True], "Fresh Pi startup must resolve once without blocking"
    config = json.loads(
        Path(captured["spec"].env[pi_native_bridge.PI_NATIVE_CONFIG_ENV_VAR]).read_text()
    )
    tool_names = {tool["name"] for tool in config["tools"]}
    assert {"list_comments", "sys_session_list"} <= tool_names
    # The fresh terminal is surfaced on the live stream for the Terminal toggle.
    assert any(evt.get("type") == "session.resource.created" for evt in published)
    task = runner_app_mod._AUTO_FORWARDER_TASKS.pop("47f049b9d13df4db397c7f46859b825f")
    assert task.get_name() == "pi-auth-refresh-47f049b9d13df4db397c7f46859b825f"
    await task
    bridge_dir = Path(captured["spec"].env[pi_native_bridge.PI_NATIVE_CONFIG_ENV_VAR]).parent
    assert supervised == [(bridge_dir, "http://127.0.0.1:8000")]

    args = captured["spec"].args
    assert args[-2:] == ["--append-system-prompt", "User addition"]
    if instruction_spec in {"raw", "resolved", "resolved-replace"}:
        assert args.count("--append-system-prompt") == 2
        prompt_flag = (
            "--system-prompt"
            if instruction_spec == "resolved-replace"
            else "--append-system-prompt"
        )
        path = Path(args[args.index(prompt_flag) + 1])
        assert ("--no-context-files" in args) is (instruction_spec == "resolved-replace")
        if instruction_spec == "resolved-replace":
            assert args[args.index("--append-system-prompt") + 1] == ""
        assert (
            path.parent
            == Path(captured["spec"].env[pi_native_bridge.PI_NATIVE_CONFIG_ENV_VAR]).parent
        )
        assert path.read_text() == instructions
        assert instructions not in args
    else:
        assert args.count("--append-system-prompt") == 1
        assert not instructions_path.exists()


@pytest.mark.asyncio
async def test_auto_create_pi_terminal_surfaces_credential_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider with an unresolved credential posts a visible session notice.

    Without this, a Pi session whose Databricks token can't be refreshed
    launches but every message silently fails to reach the model — no reply,
    no reason. The warning is delivered as an ``error`` item (which the web UI
    renders as a distinct banner) via ``external_conversation_item``.
    """
    import omnigent.harnesses.pi_native.bridge as pi_native_bridge
    import omnigent.harnesses.pi_native.credentials as pi_native_credentials
    import omnigent.harnesses.pi_native.main as pi_native

    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(pi_native_bridge, "_BRIDGE_ROOT", tmp_path / "pi-bridge")
    monkeypatch.setattr(pi_native, "resolve_pi_executable", lambda: "pi")

    provider = pi_native_credentials.PiProviderConfig(
        provider_id="omnigent",
        base_url="https://wkspc.example.com/ai-gateway/anthropic",
        api="anthropic-messages",
        model="databricks-claude-sonnet-4-6",
        api_key="!databricks auth token --profile demo-staging",
        auth_header=True,
        credential_warning="⚠️ Couldn't authenticate to `demo-staging`; run databricks auth login.",
    )
    monkeypatch.setattr(
        pi_native_credentials, "resolve_pi_native_provider", lambda **_kwargs: provider
    )
    # Avoid touching the real Pi settings/models writer; the launch env is not
    # under test here — the credential-warning surfacing is.
    monkeypatch.setattr(
        pi_native_credentials,
        "pi_native_provider_launch",
        lambda _agent_dir, _provider, _effort=None, **_kwargs: (
            pi_native_credentials.PiNativeLaunch(env={}, args=[])
        ),
    )

    async def _fake_launch_config(**_kwargs: Any) -> _PiNativeLaunchConfig:
        return _PiNativeLaunchConfig(
            workspace=tmp_path,
            server_url="http://127.0.0.1:8000",
            terminal_launch_args=None,
            external_session_id=None,
        )

    monkeypatch.setattr("omnigent.runner.app._pi_native_launch_config", _fake_launch_config)

    class _FakeResourceRegistry:
        terminal_registry = None

        async def launch_required_terminal(
            self, *, session_id: str, terminal_name: str, **_kwargs: Any
        ) -> SessionResourceView:
            del terminal_name, _kwargs
            return SessionResourceView(
                id="terminal_pi_main",
                type="terminal",
                session_id=session_id,
                name="pi:main",
                metadata={"terminal_name": "pi", "session_key": "main", "running": True},
            )

    posts: list[tuple[str, dict[str, Any]]] = []

    class _RecordingServerClient(NullServerClient):
        async def post(self, url: str, **kwargs: Any) -> NullServerClient._Response:
            posts.append((url, kwargs.get("json") or {}))
            return self._Response()

    await _auto_create_pi_terminal(
        _PI_SESSION_ID,
        _FakeResourceRegistry(),  # type: ignore[arg-type]
        lambda _sid, _evt: None,
        server_client=_RecordingServerClient(),  # type: ignore[arg-type]
    )

    warning_posts = [
        body
        for url, body in posts
        if "/events" in url and body.get("type") == "external_conversation_item"
    ]
    assert len(warning_posts) == 1
    data = warning_posts[0]["data"]
    assert data["item_type"] == "error"
    assert data["item_data"]["code"] == "pi_credentials_unresolved"
    assert "databricks auth login" in data["item_data"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("effort_warning", "model_notice", "expected_code"),
    [
        (
            "effort ignored for gateway-routed model system.ai.placeholder-model-xyz: "
            "thinking disabled to keep text surfacing",
            None,
            "pi_native_effort_ignored",
        ),
        (
            None,
            "The model override 'other-gw/system.ai.placeholder-model-xyz' names provider "
            "'other-gw', but this Pi session is served by provider 'gw'; the model "
            "'system.ai.placeholder-model-xyz' was requested from 'gw' instead.",
            "pi_native_model_rerouted",
        ),
    ],
)
async def test_auto_create_pi_terminal_informational_notice_posts_info_level(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    effort_warning: str | None,
    model_notice: str | None,
    expected_code: str,
) -> None:
    """An informational launch notice posts at info level, not as an error.

    An effort setting dropped for a gateway-routed model, or a pick qualified by
    another configured provider that is requested from the serving one, leaves
    the session working. Each must arrive as level=info with its own code, not
    as the destructive pi_credentials_unresolved banner that counts as a failure.
    """
    import omnigent.harnesses.pi_native.bridge as pi_native_bridge
    import omnigent.harnesses.pi_native.credentials as pi_native_credentials
    import omnigent.harnesses.pi_native.main as pi_native

    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(pi_native_bridge, "_BRIDGE_ROOT", tmp_path / "pi-bridge")
    monkeypatch.setattr(pi_native, "resolve_pi_executable", lambda: "pi")

    provider = pi_native_credentials.PiProviderConfig(
        provider_id="omnigent",
        base_url="https://wkspc.example.com/ai-gateway/responses",
        api="openai-responses",
        model="system.ai.placeholder-model-xyz",
        api_key="managed-token",
        auth_header=True,
        model_notice=model_notice,
    )
    monkeypatch.setattr(
        pi_native_credentials, "resolve_pi_native_provider", lambda **_kwargs: provider
    )
    monkeypatch.setattr(
        pi_native_credentials,
        "pi_native_provider_launch",
        lambda _agent_dir, _provider, _effort=None, **_kwargs: (
            pi_native_credentials.PiNativeLaunch(env={}, args=[], effort_warning=effort_warning)
        ),
    )

    async def _fake_launch_config(**_kwargs: Any) -> _PiNativeLaunchConfig:
        return _PiNativeLaunchConfig(
            workspace=tmp_path,
            server_url="http://127.0.0.1:8000",
            terminal_launch_args=None,
            external_session_id=None,
        )

    monkeypatch.setattr("omnigent.runner.app._pi_native_launch_config", _fake_launch_config)

    class _FakeResourceRegistry:
        terminal_registry = None

        async def launch_required_terminal(
            self, *, session_id: str, terminal_name: str, **_kwargs: Any
        ) -> SessionResourceView:
            del terminal_name, _kwargs
            return SessionResourceView(
                id="terminal_pi_main",
                type="terminal",
                session_id=session_id,
                name="pi:main",
                metadata={"terminal_name": "pi", "session_key": "main", "running": True},
            )

    posts: list[tuple[str, dict[str, Any]]] = []

    class _RecordingServerClient(NullServerClient):
        async def post(self, url: str, **kwargs: Any) -> NullServerClient._Response:
            posts.append((url, kwargs.get("json") or {}))
            return self._Response()

    await _auto_create_pi_terminal(
        "5e3a1b2c4d6f7e8091a2b3c4d5e6f708",
        _FakeResourceRegistry(),  # type: ignore[arg-type]
        lambda _sid, _evt: None,
        server_client=_RecordingServerClient(),  # type: ignore[arg-type]
    )

    notice_posts = [
        body
        for url, body in posts
        if "/events" in url and body.get("type") == "external_conversation_item"
    ]
    assert len(notice_posts) == 1
    data = notice_posts[0]["data"]
    assert data["item_type"] == "error"
    assert data["item_data"]["code"] == expected_code
    assert data["item_data"]["message"] == (effort_warning or model_notice)
    assert data["item_data"]["level"] == "info"


@pytest.mark.asyncio
async def test_auto_create_pi_terminal_unmanaged_keeps_pinned_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no managed provider, a pinned model still reaches the pi argv.

    Pi falls back to its own login when ``resolve_pi_native_provider()`` is
    ``None``, but the session's pinned model must not be silently dropped:
    it passes through as ``--model`` so Pi resolves it against its own
    catalog instead of opening its default model.
    """
    import omnigent.harnesses.pi_native.bridge as pi_native_bridge
    import omnigent.harnesses.pi_native.credentials as pi_native_credentials
    import omnigent.harnesses.pi_native.main as pi_native

    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(pi_native_bridge, "_BRIDGE_ROOT", tmp_path / "pi-bridge")
    monkeypatch.setattr(pi_native, "resolve_pi_executable", lambda: "pi")
    monkeypatch.setattr(
        pi_native_credentials, "resolve_pi_native_provider", lambda **_kwargs: None
    )

    async def _fake_launch_config(**_kwargs: Any) -> _PiNativeLaunchConfig:
        return _PiNativeLaunchConfig(
            workspace=tmp_path,
            server_url="http://127.0.0.1:8000",
            terminal_launch_args=None,
            external_session_id=None,
            model_override="anthropic/claude-sonnet-4-5",
        )

    monkeypatch.setattr("omnigent.runner.app._pi_native_launch_config", _fake_launch_config)

    captured: dict[str, Any] = {}

    class _FakeResourceRegistry:
        terminal_registry = None

        async def launch_required_terminal(
            self, *, session_id: str, spec: Any, **_kwargs: Any
        ) -> SessionResourceView:
            captured["spec"] = spec
            return SessionResourceView(
                id="terminal_pi_main",
                type="terminal",
                session_id=session_id,
                name="pi:main",
                metadata={"terminal_name": "pi", "session_key": "main", "running": True},
            )

    await _auto_create_pi_terminal(
        _PI_SESSION_ID,
        _FakeResourceRegistry(),  # type: ignore[arg-type]
        lambda _sid, _evt: None,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    args = captured["spec"].args
    assert "--model" in args
    assert args[args.index("--model") + 1] == "anthropic/claude-sonnet-4-5"
    # No managed provider was injected — Pi runs on its own login.
    assert "--provider" not in args


@pytest.mark.asyncio
async def test_auto_create_pi_terminal_unmanaged_refuses_slash_bearing_managed_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A managed slash-bearing pick never reaches the own-login pi argv.

    ``omnigent/moonshotai/kimi-k2.5`` is unresolvable without the managed
    provider, and stripping the prefix would leave ``moonshotai/kimi-k2.5``,
    which Pi's ``--model`` parser mis-routes (leading segment read as a
    provider). The launch must omit ``--model`` so Pi keeps its own default.
    """
    import omnigent.harnesses.pi_native.bridge as pi_native_bridge
    import omnigent.harnesses.pi_native.credentials as pi_native_credentials
    import omnigent.harnesses.pi_native.main as pi_native

    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(pi_native_bridge, "_BRIDGE_ROOT", tmp_path / "pi-bridge")
    monkeypatch.setattr(pi_native, "resolve_pi_executable", lambda: "pi")
    monkeypatch.setattr(
        pi_native_credentials, "resolve_pi_native_provider", lambda **_kwargs: None
    )

    async def _fake_launch_config(**_kwargs: Any) -> _PiNativeLaunchConfig:
        return _PiNativeLaunchConfig(
            workspace=tmp_path,
            server_url="http://127.0.0.1:8000",
            terminal_launch_args=None,
            external_session_id=None,
            model_override="omnigent/moonshotai/kimi-k2.5",
        )

    monkeypatch.setattr("omnigent.runner.app._pi_native_launch_config", _fake_launch_config)

    captured: dict[str, Any] = {}

    class _FakeResourceRegistry:
        terminal_registry = None

        async def launch_required_terminal(
            self, *, session_id: str, spec: Any, **_kwargs: Any
        ) -> SessionResourceView:
            captured["spec"] = spec
            return SessionResourceView(
                id="terminal_pi_main",
                type="terminal",
                session_id=session_id,
                name="pi:main",
                metadata={"terminal_name": "pi", "session_key": "main", "running": True},
            )

    await _auto_create_pi_terminal(
        _PI_SESSION_ID,
        _FakeResourceRegistry(),  # type: ignore[arg-type]
        lambda _sid, _evt: None,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    args = captured["spec"].args
    assert "--model" not in args
    assert "--provider" not in args


def _pi_launch_test_stubs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probed_version: tuple[int, int, int] | None,
) -> dict[str, Any]:
    """Stub the Pi launch environment; return the registry's capture dict.

    :param tmp_path: Pytest-provided temporary directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    :param probed_version: Version the ``pi --version`` probe reports.
    :returns: Dict the fake registry fills when a terminal launches.
    """
    import omnigent.harnesses.pi_native.bridge as pi_native_bridge
    import omnigent.harnesses.pi_native.credentials as pi_native_credentials
    import omnigent.harnesses.pi_native.main as pi_native

    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(pi_native_bridge, "_BRIDGE_ROOT", tmp_path / "pi-bridge")
    monkeypatch.setattr(pi_native, "resolve_pi_executable", lambda: "pi")
    monkeypatch.setattr(pi_native, "pi_version", lambda _exe: probed_version)
    monkeypatch.setattr(
        pi_native_credentials, "resolve_pi_native_provider", lambda **_kwargs: None
    )

    async def _fake_launch_config(**_kwargs: Any) -> _PiNativeLaunchConfig:
        return _PiNativeLaunchConfig(
            workspace=tmp_path,
            server_url="http://127.0.0.1:8000",
            terminal_launch_args=None,
            external_session_id=None,
        )

    monkeypatch.setattr("omnigent.runner.app._pi_native_launch_config", _fake_launch_config)

    captured: dict[str, Any] = {}

    class _FakeResourceRegistry:
        terminal_registry = None

        async def launch_required_terminal(
            self, *, session_id: str, spec: Any, **_kwargs: Any
        ) -> SessionResourceView:
            captured["spec"] = spec
            return SessionResourceView(
                id="terminal_pi_main",
                type="terminal",
                session_id=session_id,
                name="pi:main",
                metadata={"terminal_name": "pi", "session_key": "main", "running": True},
            )

    captured["registry"] = _FakeResourceRegistry()
    return captured


@pytest.mark.asyncio
async def test_auto_create_pi_terminal_outdated_cli_fails_with_upgrade_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Pi CLI below the floor fails the launch with the upgrade path named.

    An old ``pi`` exits 1 on ``--provider omnigent``; the launch must refuse
    before spawning anything and name the installed version, the floor, and
    the installer's upgrade command instead of a generic start failure.
    """
    from omnigent.harnesses.pi_native.main import PiNativeCliOutdatedError
    from omnigent.onboarding.harness_install import (
        PI_KEY,
        harness_install_display,
        harness_install_spec,
    )

    captured = _pi_launch_test_stubs(tmp_path, monkeypatch, (0, 83, 0))

    with pytest.raises(PiNativeCliOutdatedError) as excinfo:
        await _auto_create_pi_terminal(
            _PI_SESSION_ID,
            captured["registry"],  # type: ignore[arg-type]
            lambda _sid, _evt: None,
            server_client=NullServerClient(),  # type: ignore[arg-type]
        )

    message = str(excinfo.value)
    min_version = harness_install_spec(PI_KEY)
    assert min_version is not None and min_version.min_version is not None
    assert "0.83.0" in message
    assert min_version.min_version in message
    assert harness_install_display(PI_KEY) in message
    # The launch was refused before any terminal was spawned.
    assert "spec" not in captured


@pytest.mark.asyncio
@pytest.mark.parametrize("version_kind", ["at_floor", "unknown"])
async def test_auto_create_pi_terminal_cli_version_at_floor_or_unknown_proceeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version_kind: str,
) -> None:
    """A Pi at the floor — or an unprobeable version — launches as today.

    The floor is inclusive, and an unknown probe result must fail open so a
    Pi whose ``--version`` output changes shape keeps working.
    """
    from omnigent.onboarding.harness_install import PI_KEY, harness_install_spec

    probed: tuple[int, int, int] | None = None
    if version_kind == "at_floor":
        spec = harness_install_spec(PI_KEY)
        assert spec is not None and spec.min_version is not None
        floor_parts = [int(part) for part in spec.min_version.split(".")]
        probed = (floor_parts[0], floor_parts[1], floor_parts[2])
    captured = _pi_launch_test_stubs(tmp_path, monkeypatch, probed)

    await _auto_create_pi_terminal(
        _PI_SESSION_ID,
        captured["registry"],  # type: ignore[arg-type]
        lambda _sid, _evt: None,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    assert captured["spec"].command == "pi"


@pytest.mark.parametrize(
    (
        "project",
        "global_file",
        "user_args",
        "mode",
        "approve",
        "authored",
        "unreadable",
        "expected",
    ),
    [
        (True, True, [], "append", True, True, None, ["project", "authored"]),
        (False, True, [], "append", True, True, None, ["global", "authored"]),
        (False, False, [], "append", True, True, None, ["authored"]),
        (True, True, [], "append", False, True, None, ["global", "authored"]),
        (True, True, ["--no-approve"], "append", True, True, None, ["global", "authored"]),
        (True, True, ["-na", "-a"], "append", True, True, None, ["project", "authored"]),
        (
            True,
            True,
            ["--append-system-prompt", "custom"],
            "append",
            True,
            True,
            None,
            ["authored", "custom"],
        ),
        (True, True, ["--append-system-prompt", ""], "append", True, True, None, ["authored", ""]),
        (True, True, ["--system-prompt", "User base"], "replace", True, True, None, [""]),
        (True, True, [], "append", True, False, None, []),
        (True, True, [], "append", True, True, "project", ["global", "authored"]),
        (False, True, [], "append", True, True, "global", ["authored"]),
    ],
    ids=[
        "project",
        "global-fallback",
        "missing",
        "no-trust-override",
        "no-approve",
        "trust-last-wins",
        "user-append",
        "user-empty-append",
        "replace-user-base",
        "no-authored",
        "project-unreadable",
        "global-unreadable",
    ],
)
def test_native_pi_preserves_append_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    project: bool,
    global_file: bool,
    user_args: list[str],
    mode: str,
    approve: bool,
    authored: bool,
    unreadable: str | None,
    expected: list[str],
) -> None:
    workspace = tmp_path / "workspace with spaces"
    project_config = workspace / ".pi"
    agent_dir = tmp_path / "active-agent"
    project_config.mkdir(parents=True)
    agent_dir.mkdir()
    paths = {
        "project": project_config / "APPEND_SYSTEM.md",
        "global": agent_dir / "APPEND_SYSTEM.md",
        "authored": tmp_path / "authored.md",
    }
    for name, present in (("project", project), ("global", global_file), ("authored", authored)):
        if present:
            paths[name].write_text(name)
    if unreadable is not None:
        original_stat = Path.stat

        def stat(path: Path, *args: Any, **kwargs: Any) -> Any:
            if path == paths[unreadable]:
                raise PermissionError("Parent directory is not searchable")
            return original_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", stat)
    args = _build_pi_native_args(
        terminal_launch_args=user_args,
        extension_path=tmp_path / "extension.js",
        session_dir=tmp_path,
        external_session_id=None,
        instructions_path=paths["authored"] if authored else None,
        context_files=False,
        system_prompt_mode=mode,
        workspace=workspace,
        agent_dir=agent_dir,
        approve=approve,
    )
    assert [
        args[index + 1] for index, arg in enumerate(args) if arg == "--append-system-prompt"
    ] == [str(paths[value].resolve()) if value in paths else value for value in expected]
    if user_args:
        assert args[-len(user_args) :] == user_args
    if mode == "replace":
        assert args[args.index("--system-prompt") + 1] == str(paths["authored"])
        assert args[-2:] == ["--system-prompt", "User base"]


async def _launch_pi_terminal_args(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_override: str | None,
    agent_spec: AgentSpec | None = None,
) -> list[str]:
    import omnigent.harnesses.pi_native.bridge as pi_native_bridge
    import omnigent.harnesses.pi_native.credentials as pi_native_credentials
    import omnigent.harnesses.pi_native.main as pi_native

    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(pi_native_bridge, "_BRIDGE_ROOT", tmp_path / "pi-bridge")
    monkeypatch.setattr(pi_native, "resolve_pi_executable", lambda: "pi")
    own_login_dir = tmp_path / "pi-own-login"
    own_login_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(own_login_dir))

    config = {
        "providers": {
            "openrouter": {
                "kind": "gateway",
                "default": "pi",
                "openai": {
                    "base_url": "https://openrouter.ai/api/v1",
                    "api_key": "sk-or-testkey",
                    "wire_api": "chat",
                },
            }
        }
    }
    real_resolve = pi_native_credentials.resolve_pi_native_provider
    monkeypatch.setattr(
        pi_native_credentials,
        "resolve_pi_native_provider",
        lambda **kwargs: real_resolve(config_loader=lambda: config, **kwargs),
    )

    async def _fake_launch_config(**_kwargs: Any) -> _PiNativeLaunchConfig:
        return _PiNativeLaunchConfig(
            workspace=tmp_path,
            server_url="http://127.0.0.1:8000",
            terminal_launch_args=None,
            external_session_id=None,
            model_override=model_override,
        )

    monkeypatch.setattr("omnigent.runner.app._pi_native_launch_config", _fake_launch_config)

    captured: dict[str, Any] = {}

    class _FakeResourceRegistry:
        terminal_registry = None

        async def launch_required_terminal(
            self, *, session_id: str, spec: Any, **_kwargs: Any
        ) -> SessionResourceView:
            captured["spec"] = spec
            return SessionResourceView(
                id="terminal_pi_main",
                type="terminal",
                session_id=session_id,
                name="pi:main",
                metadata={"terminal_name": "pi", "session_key": "main", "running": True},
            )

    await _auto_create_pi_terminal(
        _PI_SESSION_ID,
        _FakeResourceRegistry(),  # type: ignore[arg-type]
        lambda _sid, _evt: None,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        agent_spec=agent_spec,
    )
    args = captured["spec"].args
    assert isinstance(args, list)
    return args


@pytest.mark.asyncio
async def test_auto_create_pi_terminal_spec_pinned_own_login_reference_passes_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_pi_own_login_catalog(tmp_path / "pi-own-login", "openai-codex", ["gpt-5.6-sol"])

    args = await _launch_pi_terminal_args(
        tmp_path,
        monkeypatch,
        model_override=None,
        agent_spec=AgentSpec(
            spec_version=1,
            name="pi",
            executor=ExecutorSpec(model="openai-codex/gpt-5.6-sol"),
        ),
    )

    assert "omnigent/openai-codex/gpt-5.6-sol" not in args
    assert "--provider" not in args
    assert "--model" in args
    assert args[args.index("--model") + 1] == "openai-codex/gpt-5.6-sol"


@pytest.mark.asyncio
async def test_auto_create_pi_terminal_keeps_gateway_routing_for_slash_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnigent.harnesses.pi_native.bridge as pi_native_bridge

    monkeypatch.setattr(pi_native_bridge, "_BRIDGE_ROOT", tmp_path / "pi-bridge")
    agent_dir = pi_native_bridge.prepare_bridge_dir(_PI_SESSION_ID) / "pi-agent"
    agent_dir.mkdir()
    append_file = agent_dir / "APPEND_SYSTEM.md"
    append_file.write_text("Active global prompt")
    own_login = tmp_path / "pi-own-login"
    own_login.mkdir()
    (own_login / "APPEND_SYSTEM.md").write_text("Inactive global prompt")
    args = await _launch_pi_terminal_args(
        tmp_path,
        monkeypatch,
        model_override="openai/gpt-4o-mini",
        agent_spec=AgentSpec(
            spec_version=1, name="pi", executor=ExecutorSpec(), instructions="Authored role"
        ),
    )
    assert args[args.index("--append-system-prompt") + 1] == str(append_file)
    assert str(own_login / "APPEND_SYSTEM.md") not in args

    assert "--provider" in args
    assert "--model" in args
    assert args[args.index("--provider") + 1] == "omnigent"
    assert args[args.index("--model") + 1] == "omnigent/openai/gpt-4o-mini"


@pytest.mark.asyncio
async def test_auto_create_kiro_terminal_launches_required_terminal_with_isolated_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kiro-native auto-create launches the TUI and session forwarder."""
    import omnigent.harnesses.kiro_native.main as kiro_native

    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:6767")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setattr(kiro_native_bridge, "_BRIDGE_ROOT", tmp_path / "kiro-bridge")
    monkeypatch.setattr(
        kiro_native,
        "resolve_kiro_executable",
        lambda **_kwargs: "/usr/bin/kiro-cli",
    )
    forwarder_calls: list[dict[str, Any]] = []
    permission_mirror_calls: list[dict[str, Any]] = []

    async def _fake_supervise_kiro_session_forwarder(**kwargs: Any) -> None:
        forwarder_calls.append(kwargs)

    async def _fake_supervise_kiro_permission_mirror(**kwargs: Any) -> None:
        permission_mirror_calls.append(kwargs)

    relay_calls: list[dict[str, Any]] = []

    async def _spy_ensure_relay(session_id: str, **kwargs: Any) -> None:
        relay_calls.append({"session_id": session_id, **kwargs})

    monkeypatch.setattr(
        "omnigent.harnesses.kiro_native.session_forwarder.supervise_kiro_session_forwarder",
        _fake_supervise_kiro_session_forwarder,
    )
    monkeypatch.setattr(
        "omnigent.harnesses.kiro_native.permissions.supervise_kiro_permission_mirror",
        _fake_supervise_kiro_permission_mirror,
    )

    async def _fake_launch_config(**_kwargs: Any) -> _KiroNativeLaunchConfig:
        return _KiroNativeLaunchConfig(
            workspace=tmp_path,
            terminal_launch_args=["--model", "auto", "--effort", "high", "hello"],
            external_session_id="kiro-session-123",
        )

    monkeypatch.setattr("omnigent.runner.app._kiro_native_launch_config", _fake_launch_config)

    captured: dict[str, Any] = {}

    class _FakeResourceRegistry:
        """Records the launch; exposes ONLY the required-terminal launch API."""

        terminal_registry = None

        async def launch_required_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            del parent_os_env
            captured["terminal_name"] = terminal_name
            captured["session_key"] = session_key
            captured["resource_role"] = resource_role
            captured["spec"] = spec
            return SessionResourceView(
                id="terminal_kiro_main",
                type="terminal",
                session_id=session_id,
                name="kiro:main",
                metadata={"terminal_name": "kiro", "session_key": "main", "running": True},
            )

    published: list[dict[str, Any]] = []

    await _auto_create_kiro_terminal(
        "823dbd1aab969b5a813fac59bb977a77",
        _FakeResourceRegistry(),  # type: ignore[arg-type]
        lambda _sid, evt: published.append(evt),
        server_client=NullServerClient(),  # type: ignore[arg-type]
        ensure_comment_relay=_spy_ensure_relay,
    )
    for _ in range(20):
        if forwarder_calls and permission_mirror_calls:
            break
        await asyncio.sleep(0)

    spec = captured["spec"]
    assert captured["terminal_name"] == "kiro"
    assert captured["session_key"] == "main"
    assert captured["resource_role"] == KIRO_NATIVE_TERMINAL_ROLE
    assert spec.command == "/usr/bin/kiro-cli"
    assert spec.args == [
        "chat",
        "--tui",
        "--resume-id",
        "kiro-session-123",
        "--model",
        "auto",
        "--effort",
        "high",
        "hello",
    ]
    assert spec.inherit_env is False
    assert "OPENAI_API_KEY" not in spec.env
    assert "OPENAI_API_KEY" in spec.env_unset
    assert spec.env[kiro_native_bridge.KIRO_NATIVE_BRIDGE_DIR_ENV_VAR] == str(
        kiro_native_bridge.bridge_dir_for_session_id("823dbd1aab969b5a813fac59bb977a77")
    )
    assert spec.env[kiro_native_bridge.KIRO_ACP_RECORD_PATH_ENV_VAR] == str(
        kiro_native_bridge.acp_record_path(
            kiro_native_bridge.bridge_dir_for_session_id("823dbd1aab969b5a813fac59bb977a77")
        )
    )
    assert any(evt.get("type") == "session.resource.created" for evt in published)
    assert forwarder_calls
    assert forwarder_calls[0]["base_url"] == "http://127.0.0.1:6767"
    assert forwarder_calls[0]["session_id"] == "823dbd1aab969b5a813fac59bb977a77"
    assert forwarder_calls[0]["agent_name"] == "kiro-native-ui"
    assert forwarder_calls[0]["workspace"] == str(tmp_path)
    assert permission_mirror_calls
    assert permission_mirror_calls[0]["base_url"] == "http://127.0.0.1:6767"
    assert permission_mirror_calls[0]["session_id"] == "823dbd1aab969b5a813fac59bb977a77"
    # The Omnigent MCP tool relay is seeded for this session's bridge dir.
    assert relay_calls == [
        {
            "session_id": "823dbd1aab969b5a813fac59bb977a77",
            "explicit_bridge_dir": kiro_native_bridge.bridge_dir_for_session_id(
                "823dbd1aab969b5a813fac59bb977a77"
            ),
            "await_notify": False,
        }
    ]
    # And the Omnigent MCP server is declared in the workspace-scoped kiro config.
    workspace_mcp = tmp_path / ".kiro" / "settings" / "mcp.json"
    assert workspace_mcp.exists()
    mcp_servers = json.loads(workspace_mcp.read_text())["mcpServers"]
    assert "serve-mcp" in mcp_servers["omnigent"]["args"]


@pytest.mark.asyncio
async def test_auto_create_kiro_terminal_skips_mcp_wiring_without_relay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a comment-relay callback, the Omnigent MCP is NOT wired.

    The workspace mcp.json write + relay seed are gated on ``server_client`` AND
    ``ensure_comment_relay`` together, so serve-mcp never launches with no relay
    to route calls back to. With ``ensure_comment_relay`` absent the gate must
    short-circuit: no workspace ``mcp.json`` is written.
    """
    import omnigent.harnesses.kiro_native.main as kiro_native

    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:6767")
    monkeypatch.setattr(kiro_native_bridge, "_BRIDGE_ROOT", tmp_path / "kiro-bridge")
    monkeypatch.setattr(
        kiro_native,
        "resolve_kiro_executable",
        lambda **_kwargs: "/usr/bin/kiro-cli",
    )

    async def _noop_supervise(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "omnigent.harnesses.kiro_native.session_forwarder.supervise_kiro_session_forwarder",
        _noop_supervise,
    )
    monkeypatch.setattr(
        "omnigent.harnesses.kiro_native.permissions.supervise_kiro_permission_mirror",
        _noop_supervise,
    )
    mcp_writes: list[Any] = []
    monkeypatch.setattr(
        kiro_native_bridge,
        "write_kiro_workspace_mcp_config",
        lambda *args, **kwargs: mcp_writes.append((args, kwargs)),
    )

    async def _fake_launch_config(**_kwargs: Any) -> _KiroNativeLaunchConfig:
        return _KiroNativeLaunchConfig(
            workspace=tmp_path,
            terminal_launch_args=["hello"],
            external_session_id=None,
        )

    monkeypatch.setattr("omnigent.runner.app._kiro_native_launch_config", _fake_launch_config)

    class _FakeResourceRegistry:
        terminal_registry = None

        async def launch_required_terminal(
            self, *, session_id: str, **_kwargs: Any
        ) -> SessionResourceView:
            return SessionResourceView(
                id="terminal_kiro_main",
                type="terminal",
                session_id=session_id,
                name="kiro:main",
                metadata={"terminal_name": "kiro", "session_key": "main", "running": True},
            )

    # No ``ensure_comment_relay`` argument -> the MCP wiring gate stays closed.
    await _auto_create_kiro_terminal(
        "17b3209fb5684c628f95edee0042e455",
        _FakeResourceRegistry(),  # type: ignore[arg-type]
        lambda _sid, _evt: None,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    assert mcp_writes == []
    assert not (tmp_path / ".kiro" / "settings" / "mcp.json").exists()


@pytest.mark.asyncio
async def test_auto_create_pi_terminal_inherits_agent_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Pi-native auto-create must honour the agent's ``os_env.sandbox``.

    Regression for the sandbox-override bug: the pi-native auto-create path
    built a fresh ``TerminalEnvSpec`` whose ``os_env`` carried no ``sandbox``
    and passed no ``parent_os_env``, so ``launch_required_terminal`` fell back
    to ``_default_sandbox_for_platform`` (``linux_bwrap`` on Linux) and ignored
    the agent YAML.  A pi-native agent that declares
    ``os_env.sandbox.type: none`` was wrongly forced into bwrap, which failed
    on a hardened host.

    Asserts the launched terminal spec's ``os_env.sandbox`` is the agent's
    ``none`` sandbox (not the platform default) and that the agent's ``os_env``
    is threaded through as the launch ``parent_os_env`` so the rest of the
    policy (egress_rules / env_passthrough) is also inherited.

    :param tmp_path: Pytest-provided temporary directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    import omnigent.harnesses.pi_native.bridge as pi_native_bridge
    import omnigent.harnesses.pi_native.credentials as pi_native_credentials
    import omnigent.harnesses.pi_native.main as pi_native
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(pi_native_bridge, "_BRIDGE_ROOT", tmp_path / "pi-bridge")
    monkeypatch.setattr(pi_native, "resolve_pi_executable", lambda: "pi")
    # Accept the ``model`` kwarg the runner now threads through (the spec model
    # → models.json path); None still skips provider injection here.
    monkeypatch.setattr(
        pi_native_credentials, "resolve_pi_native_provider", lambda **_kwargs: None
    )

    async def _fake_launch_config(**_kwargs: Any) -> _PiNativeLaunchConfig:
        return _PiNativeLaunchConfig(
            workspace=tmp_path,
            server_url="http://127.0.0.1:8000",
            terminal_launch_args=None,
            external_session_id=None,
        )

    monkeypatch.setattr("omnigent.runner.app._pi_native_launch_config", _fake_launch_config)

    captured: dict[str, Any] = {}

    class _FakeResourceRegistry:
        """Captures the launched terminal spec and parent os_env."""

        terminal_registry = None

        async def launch_required_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """Record the spec + parent_os_env and return a resource view."""
            del terminal_name, session_key
            captured["spec"] = spec
            captured["parent_os_env"] = parent_os_env
            return SessionResourceView(
                id="terminal_pi_main",
                type="terminal",
                session_id=session_id,
                name="pi:main",
                metadata={"terminal_name": "pi", "session_key": "main", "running": True},
            )

    # An agent that declares sandbox: none (runs unconfined; outer
    # container/VM is the security boundary).
    agent_os_env = OSEnvSpec(
        type="caller_process",
        cwd=".",
        sandbox=OSEnvSandboxSpec(type="none"),
    )
    agent_spec = AgentSpec(
        spec_version=1,
        name="pi_code",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "pi-native", "model": "pi-default"},
        ),
        os_env=agent_os_env,
    )

    await _auto_create_pi_terminal(
        "28a25c47fe4fd8ccde95c80bab47c1c7",
        _FakeResourceRegistry(),  # type: ignore[arg-type]
        lambda _sid, _evt: None,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        agent_spec=agent_spec,
    )

    launched_sandbox = captured["spec"].os_env.sandbox
    assert launched_sandbox is not None, (
        "auto-create dropped the agent's sandbox; launch_required_terminal will "
        "fall back to _default_sandbox_for_platform (bwrap), overriding sandbox: none"
    )
    assert launched_sandbox.type == "none"
    # The whole os_env is threaded through as the inheritance parent.
    assert captured["parent_os_env"] is agent_os_env


class _TerminalLaunchCaptured(Exception):
    """Raised by the fake registry once the terminal launch is recorded."""


def _stub_goose_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import omnigent.harnesses.goose_native.bridge as bridge
    import omnigent.harnesses.goose_native.main as main

    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "goose-bridge")
    monkeypatch.setattr(main, "resolve_goose_executable", lambda: "goose")


def _stub_hermes_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import omnigent.harnesses.hermes_native.bridge as bridge
    import omnigent.harnesses.hermes_native.main as main

    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "hermes-bridge")
    monkeypatch.setattr(main, "resolve_hermes_executable", lambda: "hermes")


def _stub_kiro_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import omnigent.harnesses.kiro_native.main as main

    monkeypatch.setattr(kiro_native_bridge, "_BRIDGE_ROOT", tmp_path / "kiro-bridge")
    monkeypatch.setattr(main, "resolve_kiro_executable", lambda **_kwargs: "kiro-cli")

    async def _fake_launch_config(**_kwargs: Any) -> _KiroNativeLaunchConfig:
        return _KiroNativeLaunchConfig(
            workspace=tmp_path / "workspace",
            terminal_launch_args=None,
            external_session_id=None,
        )

    monkeypatch.setattr("omnigent.runner.app._kiro_native_launch_config", _fake_launch_config)


def _stub_devin_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import omnigent.harnesses.devin_native.bridge as bridge
    import omnigent.harnesses.devin_native.main as main

    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "devin-bridge")
    monkeypatch.setattr(main, "resolve_devin_executable", lambda **_kwargs: "devin")
    monkeypatch.setattr(main, "resolve_devin_launch_model", lambda _model, _effort: None)


def _stub_qwen_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import omnigent.harnesses.qwen_native.bridge as bridge
    import omnigent.harnesses.qwen_native.main as main

    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "qwen-bridge")
    monkeypatch.setattr(main, "resolve_qwen_executable", lambda: "qwen")


def _stub_kimi_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import omnigent.harnesses.kimi_native.bridge as bridge
    import omnigent.harnesses.kimi_native.credentials as credentials
    import omnigent.harnesses.kimi_native.main as main

    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "kimi-bridge")
    monkeypatch.setattr(main, "resolve_kimi_executable", lambda: "kimi")
    monkeypatch.setattr(
        credentials,
        "build_kimi_session_home",
        lambda session_home, **_kwargs: {"KIMI_CODE_HOME": str(session_home)},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("launch_path", ["hermes", "devin", "hermes_spawn"])
@pytest.mark.parametrize("dependency", ["construction", "refresh"])
async def test_native_policy_hook_setup_keeps_tunnel_pings_responsive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launch_path: str, dependency: str
) -> None:
    from omnigent.runner import _entry
    from omnigent.runner.native import orchestration
    from omnigent.runner.transports.ws_tunnel.frames import (
        PingFrame,
        PongFrame,
        decode_frame,
        encode_frame,
    )
    from omnigent.runner.transports.ws_tunnel.serve import _handle_tunnel_frame

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")
    terminal_name = "devin" if launch_path == "devin" else "hermes"
    (_stub_devin_launch if terminal_name == "devin" else _stub_hermes_launch)(
        tmp_path, monkeypatch
    )

    async def launch_config(**_kwargs: Any) -> _PiNativeLaunchConfig:
        return _PiNativeLaunchConfig(
            workspace=workspace,
            server_url="http://ap.example",
            terminal_launch_args=None,
            external_session_id=None,
        )

    monkeypatch.setattr(orchestration, "_pi_native_launch_config", launch_config)
    loop = asyncio.get_running_loop()
    auth_started = asyncio.Event()
    auth_release = threading.Event()
    auth_unblocked_by_ping: list[bool] = []
    sent: list[str] = []

    def wait_for_ping() -> None:
        loop.call_soon_threadsafe(auth_started.set)
        auth_unblocked_by_ping.append(auth_release.wait(timeout=budget(2)))

    def token() -> str:
        if dependency == "refresh":
            wait_for_ping()
        return "mock-policy-token"

    def make_factory(server_url: str | None = None) -> Any:
        if server_url is None:
            return None
        if dependency == "construction":
            wait_for_ping()
        return token

    monkeypatch.setattr(_entry, "_make_auth_token_factory", make_factory)

    class Registry:
        terminal_registry = None

        async def launch_required_terminal(self, **_kwargs: Any) -> None:
            raise _TerminalLaunchCaptured

    session_id = "5f0c2d7e9a8b4c6d8e1f2a3b4c5d6e7f"

    async def setup() -> None:
        if launch_path == "hermes_spawn":
            env = await orchestration._resolve_native_spawn_env(
                "hermes-native",
                session_id,
                server_client=cast(httpx.AsyncClient, NullServerClient()),
                optional_labels=None,
            )
            assert env is not None
        else:
            builder = getattr(orchestration, f"_auto_create_{terminal_name}_terminal")
            with pytest.raises(_TerminalLaunchCaptured):
                await builder(session_id, Registry(), lambda *_args: None, server_client=None)

    async def send_text(text: str) -> None:
        sent.append(text)

    task = asyncio.create_task(setup())
    try:
        await asyncio.wait_for(auth_started.wait(), timeout=budget(4))
        await _handle_tunnel_frame(
            cast(Any, None), encode_frame(PingFrame(ts=123)), send_text, {}, {}
        )
        auth_release.set()
        await task
    finally:
        auth_release.set()
        await asyncio.gather(task, return_exceptions=True)

    assert auth_unblocked_by_ping == [True], f"policy hook {dependency} blocked the event loop"
    assert decode_frame(sent[0]) == PongFrame(ts=123)
    if terminal_name == "hermes":
        from omnigent.harnesses.hermes_native import bridge as hermes_bridge

        home = hermes_bridge.read_hermes_home(hermes_bridge.bridge_dir_for_session_id(session_id))
        assert home is not None
        wrapper = home / "omnigent-policy-hook.sh"
    else:
        from omnigent.harnesses.devin_native import bridge as devin_bridge

        wrapper = devin_bridge.hook_wrapper_path(
            devin_bridge.bridge_dir_for_session_id(session_id)
        )
    wrapper_text = wrapper.read_text()
    assert "mock-policy-token" in wrapper_text
    assert session_id in wrapper_text
    assert wrapper.stat().st_mode & 0o777 == 0o700


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 3])
@pytest.mark.parametrize("writer_fails", [False, True])
async def test_cancelled_policy_writer_drains_before_a_replacement_launch(
    tmp_path: Path, cancel_count: int, writer_fails: bool
) -> None:
    from omnigent.runner.native import orchestration

    loop = asyncio.get_running_loop()
    launch_lock = asyncio.Lock()
    first_started = asyncio.Event()
    first_finished = asyncio.Event()
    release_first = threading.Event()
    replacement_attempted = asyncio.Event()
    replacement_entered = asyncio.Event()
    wrapper = tmp_path / "policy-hook.sh"
    writes: list[str] = []

    def first_writer() -> Path:
        loop.call_soon_threadsafe(first_started.set)
        try:
            if not release_first.wait(timeout=budget(5)):
                raise AssertionError("Test did not release the first policy writer")
            wrapper.write_text("cancelled launch")
            writes.append("cancelled")
            if writer_fails:
                raise OSError("synthetic write failure after cancellation")
            return wrapper
        finally:
            loop.call_soon_threadsafe(first_finished.set)

    def replacement_writer() -> Path:
        wrapper.write_text("replacement launch")
        writes.append("replacement")
        return wrapper

    async def first_launch() -> None:
        async with launch_lock:
            await orchestration._write_native_policy_hook(first_writer)

    async def replacement_launch() -> Path:
        replacement_attempted.set()
        async with launch_lock:
            replacement_entered.set()
            return await orchestration._write_native_policy_hook(replacement_writer)

    first = asyncio.create_task(first_launch())
    replacement: asyncio.Task[Path] | None = None
    try:
        await asyncio.wait_for(first_started.wait(), timeout=budget(3))
        first.cancel("cancelled policy launch")
        await asyncio.sleep(0)
        replacement = asyncio.create_task(replacement_launch())
        await asyncio.wait_for(replacement_attempted.wait(), timeout=budget(3))
        for _ in range(cancel_count - 1):
            first.cancel("repeated cancellation")
            await asyncio.sleep(0)
        assert launch_lock.locked()
        assert not first.done(), "Cancellation released a still-writing launch"
        assert not replacement_entered.is_set()
        assert not first_finished.is_set()

        release_first.set()
        with pytest.raises(asyncio.CancelledError, match="cancelled policy launch"):
            await asyncio.wait_for(first, timeout=budget(3))
        assert first_finished.is_set()
        assert await asyncio.wait_for(replacement, timeout=budget(3)) == wrapper
        assert writes == ["cancelled", "replacement"]
        assert wrapper.read_text() == "replacement launch"
        assert not launch_lock.locked()
    finally:
        release_first.set()
        await asyncio.wait_for(first_finished.wait(), timeout=budget(3))
        await asyncio.gather(
            first, *([replacement] if replacement is not None else []), return_exceptions=True
        )


@pytest.mark.asyncio
async def test_policy_writer_propagates_a_failure_without_cancellation() -> None:
    from omnigent.runner.native import orchestration

    failure = OSError("synthetic policy write failure")

    def write() -> None:
        raise failure

    with pytest.raises(OSError) as raised:
        await orchestration._write_native_policy_hook(write)
    assert raised.value is failure


@pytest.mark.parametrize(
    ("terminal_name", "builder_name", "stub_launch"),
    [
        ("goose", "_auto_create_goose_terminal", _stub_goose_launch),
        ("hermes", "_auto_create_hermes_terminal", _stub_hermes_launch),
        ("kiro", "_auto_create_kiro_terminal", _stub_kiro_launch),
        ("devin", "_auto_create_devin_terminal", _stub_devin_launch),
        ("qwen", "_auto_create_qwen_terminal", _stub_qwen_launch),
        ("kimi", "_auto_create_kimi_terminal", _stub_kimi_launch),
    ],
    ids=["goose", "hermes", "kiro", "devin", "qwen", "kimi"],
)
@pytest.mark.asyncio
async def test_auto_create_native_terminal_inherits_agent_sandbox(
    terminal_name: str,
    builder_name: str,
    stub_launch: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Goose/hermes/kiro/devin/qwen/kimi auto-create honour the agent's ``os_env.sandbox``.

    Same regression as the pi test above (#10262): these builders built the
    terminal ``OSEnvSpec`` with no ``sandbox`` and passed no ``parent_os_env``,
    so ``launch_required_terminal`` fell back to
    ``_default_sandbox_for_platform`` (``linux_bwrap`` on Linux) and an agent
    declaring ``os_env.sandbox.type: none`` failed to start on a host without
    ``bwrap``.

    The fake registry raises once it has recorded the launch, so the builders'
    post-launch forwarder/relay startup never runs; only the launch spec is
    under test.

    :param terminal_name: Native terminal name, e.g. ``"hermes"``.
    :param builder_name: ``omnigent.runner.app`` auto-create builder to call.
    :param stub_launch: Stubs the builder's pre-launch dependencies.
    :param tmp_path: Pytest-provided temporary directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.runner import app as runner_app

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda **_kwargs: None)

    async def _fake_launch_config(**_kwargs: Any) -> _PiNativeLaunchConfig:
        return _PiNativeLaunchConfig(
            workspace=workspace,
            server_url="http://127.0.0.1:8000",
            terminal_launch_args=None,
            external_session_id=None,
        )

    monkeypatch.setattr("omnigent.runner.app._pi_native_launch_config", _fake_launch_config)
    stub_launch(tmp_path, monkeypatch)

    captured: dict[str, Any] = {}

    class _FakeResourceRegistry:
        """Captures the launched terminal spec and parent os_env, then stops."""

        terminal_registry = None

        async def launch_required_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """Record the spec + parent_os_env and end the builder here."""
            del session_id, session_key, resource_role
            captured["terminal_name"] = terminal_name
            captured["spec"] = spec
            captured["parent_os_env"] = parent_os_env
            raise _TerminalLaunchCaptured

    agent_os_env = OSEnvSpec(
        type="caller_process",
        cwd=".",
        sandbox=OSEnvSandboxSpec(type="none"),
    )
    agent_spec = AgentSpec(
        spec_version=1,
        name=f"{terminal_name}_code",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": f"{terminal_name}-native"},
        ),
        os_env=agent_os_env,
    )

    builder = getattr(runner_app, builder_name)
    with pytest.raises(_TerminalLaunchCaptured):
        await builder(
            "5f0c2d7e9a8b4c6d8e1f2a3b4c5d6e7f",
            _FakeResourceRegistry(),
            lambda _sid, _evt: None,
            server_client=None,
            agent_spec=agent_spec,
        )

    assert captured["terminal_name"] == terminal_name
    launched_sandbox = captured["spec"].os_env.sandbox
    assert launched_sandbox is not None, (
        "auto-create dropped the agent's sandbox; launch_required_terminal will "
        "fall back to _default_sandbox_for_platform (bwrap), overriding sandbox: none"
    )
    assert launched_sandbox.type == "none"
    assert captured["parent_os_env"] is agent_os_env


@pytest.mark.parametrize("terminal_name", ["goose", "hermes", "kiro", "qwen"])
@pytest.mark.asyncio
async def test_native_launch_arms_resolve_agent_spec_for_terminal(
    terminal_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The create-session and ensure arms hand goose/hermes/kiro/qwen the agent spec.

    The builders can only inherit ``os_env.sandbox`` (#10262) if the launch
    context carries ``agent_spec``. Before the fix these four harnesses were
    missing from the create-session ``_launch_resolve_spec`` arm in
    ``omnigent.runner.app`` and from the ensure ``build_context`` arm in
    ``omnigent.runner.resource_routes``, so ``ctx.agent_spec`` stayed ``None``
    on both paths and the terminal fell back to the platform-default sandbox.

    :param terminal_name: Native terminal name, e.g. ``"hermes"``.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    agent_os_env = OSEnvSpec(
        type="caller_process",
        cwd=".",
        sandbox=OSEnvSandboxSpec(type="none"),
    )
    native_spec = AgentSpec(
        spec_version=1,
        name=f"{terminal_name}_code",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": f"{terminal_name}-native"},
        ),
        os_env=agent_os_env,
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return native_spec

    launched_specs: list[Any] = []

    async def _fake_launch(ctx: Any) -> SessionResourceView:
        launched_specs.append(ctx.agent_spec)
        return SessionResourceView(
            id=f"terminal_{terminal_name}_main",
            type="terminal",
            session_id=ctx.session_id,
            name=f"{terminal_name}:main",
        )

    monkeypatch.setattr(f"omnigent.runner.native._launch_{terminal_name}", _fake_launch)

    session_id = "c4b1e6f2a9d84e0b8f3a7c5d2e1b9a60"
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=TerminalRegistry(),
    )
    async with _runner_client(app) as client:
        created = await client.post(
            "/v1/sessions",
            json={"session_id": session_id, "agent_id": "9a3f0c1e7b2d4e6f8a0b1c2d3e4f5a6b"},
        )
        assert created.status_code == 201, created.text
        # The fake launch registers no terminal, so the ensure arm builds again.
        ensured = await client.post(
            f"/v1/sessions/{session_id}/resources/terminals",
            json={
                "terminal": terminal_name,
                "session_key": "main",
                "ensure_native_terminal": True,
            },
        )
        assert ensured.status_code == 200, ensured.text

    assert len(launched_specs) == 2, launched_specs
    for launched_spec in launched_specs:
        assert launched_spec is not None, "launch context carried no agent spec"
        assert _agent_os_env_from_spec(launched_spec) is agent_os_env


@pytest.mark.asyncio
@pytest.mark.parametrize("use_envelope", [False, True], ids=["legacy", "envelope"])
async def test_auto_create_claude_terminal_passes_session_effort(
    use_envelope: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Host-spawned terminal launch reads session effort and passes ``--effort``.

    When the Omnigent server returns a session with a persisted
    ``reasoning_effort``, the auto-create path must include
    ``--effort <value>`` in the Claude CLI args so the terminal
    starts at the user's chosen effort level.

    :param tmp_path: Pytest-provided temporary directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")

    workspace = tmp_path / "workspace"
    (workspace / ".omnigent").mkdir(parents=True)
    (workspace / ".omnigent" / "config.yaml").write_text(
        "harness:\n  default: claude-native\n  claude-native:\n    command: workspace-claude\n"
    )
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(workspace))
    config_home = tmp_path / "config-home"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(
        "harness:\n  claude-native:\n    command: user-claude\n"
    )
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config-home"))
    monkeypatch.delenv("OMNIGENT_CLAUDE_PATH", raising=False)

    def missing_cwd() -> Path:
        raise FileNotFoundError("process cwd was removed")

    monkeypatch.setattr(Path, "cwd", missing_cwd)

    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.forwarder.supervise_forwarder",
        _no_op_forwarder,
    )

    captured: dict[str, Any] = {}

    # Fake Omnigent server client that returns a session with reasoning_effort.
    def _handle_request(_request: httpx.Request) -> httpx.Response:
        if use_envelope:
            raise AssertionError("envelope terminal startup made a legacy HTTP callback")
        return httpx.Response(
            200,
            json={"reasoning_effort": "high", "labels": {}},
        )

    fake_client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(_handle_request),
    )

    session_id = "f89fd41f6eefee45b2117ac0fcbc73fa"
    session_init = (
        RunnerSessionInitEnvelope.model_validate(
            {
                "protocol_version": 2,
                "server_version": "0.6.0.dev0",
                "session_id": session_id,
                "agent_id": "agent",
                "snapshot": {
                    "created_at": 10,
                    "updated_at": 11,
                    "workspace": str(workspace),
                    "reasoning_effort": "high",
                    "labels": {},
                },
            }
        )
        if use_envelope
        else None
    )

    with caplog.at_level(logging.INFO, logger="omnigent.runner.app"):
        await _auto_create_claude_terminal(
            session_id,
            _RecordingClaudeRegistry(captured),
            lambda _sid, _evt: None,
            server_client=fake_client,
            session_init=session_init,
        )

    args = captured["spec"].args
    assert captured["spec"].os_env.cwd == str(workspace)
    assert captured["spec"].command == "user-claude"
    assert "--effort" in args
    effort_idx = args.index("--effort")
    assert args[effort_idx + 1] == "high"
    messages = [
        record.getMessage()
        for record in caplog.records
        if "Claude terminal bridge prepared" in record.getMessage()
    ]
    assert messages == [f"Claude terminal bridge prepared: session={session_id}"]
    assert str(bridge_dir_for_bridge_id(session_id)) not in messages[0]

    await fake_client.aclose()


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_rejects_missing_recorded_workspace_before_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing recorded workspace stops startup before bridge or terminal setup."""
    missing_workspace = tmp_path / "removed-workspace"
    session_id = "f89fd41f6eefee45b2117ac0fcbc73fa"
    session_init = RunnerSessionInitEnvelope.model_validate(
        {
            "protocol_version": 2,
            "server_version": "0.6.0.dev0",
            "session_id": session_id,
            "agent_id": "agent",
            "snapshot": {
                "created_at": 10,
                "updated_at": 11,
                "workspace": str(missing_workspace),
                "labels": {},
            },
        }
    )

    def unexpected_bridge_setup(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise AssertionError("bridge setup must not run for a missing workspace")

    monkeypatch.setattr(claude_native_bridge, "prepare_bridge_dir", unexpected_bridge_setup)

    with pytest.raises(OmnigentError) as failure:
        await _auto_create_claude_terminal(
            session_id,
            object(),  # type: ignore[arg-type]
            lambda _sid, _evt: None,
            server_client=NullServerClient(),  # type: ignore[arg-type]
            session_init=session_init,
        )

    assert failure.value.code == ErrorCode.WORKSPACE_MISSING


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_rejects_windows_native_claude_under_wsl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    WSL + a Windows-native ``claude`` on PATH must fail fast, not time out.

    With no ``OMNIGENT_CLAUDE_PATH`` / config override, this launch path
    spawns the harness default ``"claude"`` resolved against the process
    PATH. When that resolution lands on a Windows-native CLI (which cannot
    execute the WSL Python hook command), the launch must raise the
    actionable mismatch error before any terminal is spawned.
    """
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge.is_wsl", lambda: True)
    # No ambient override may leak in: the launch must resolve the bare
    # "claude" name, and config isolation keeps a developer's real
    # ``harness.claude-native.command`` out of the test.
    monkeypatch.delenv("OMNIGENT_CLAUDE_PATH", raising=False)
    config_home = tmp_path / "config-home"
    config_home.mkdir()
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))
    windows_claude = "/mnt/c/Users/example/AppData/Roaming/npm/claude.cmd"
    monkeypatch.setattr(
        "omnigent._platform.shutil.which",
        lambda name: windows_claude if name == "claude" else None,
    )

    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.forwarder.supervise_forwarder",
        _no_op_forwarder,
    )

    class _UnreachedResourceRegistry:
        """Fails the test if the launch reaches actual terminal spawn."""

        terminal_registry = None

        async def launch_required_terminal(self, **kwargs: Any) -> SessionResourceView:
            raise AssertionError(
                "terminal launch must not proceed past the interpreter-mismatch check"
            )

    def _handle_request(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"labels": {}})

    fake_client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(_handle_request),
    )

    with pytest.raises(ClaudeNativeHookInterpreterMismatchError, match="Windows-native"):
        await _auto_create_claude_terminal(
            "conv_wsl_mismatch",
            _UnreachedResourceRegistry(),
            lambda _sid, _evt: None,
            server_client=fake_client,
        )

    await fake_client.aclose()


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_rejects_windows_native_claude_env_override_under_wsl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A Windows-native ``OMNIGENT_CLAUDE_PATH`` override is rejected under WSL.

    This launch path honours the env/config command override (the same
    resolver the local CLI uses), so the override *is* the binary that
    would spawn -- a Windows-native override must be validated and
    rejected just like a PATH-resolved one.
    """
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge.is_wsl", lambda: True)
    config_home = tmp_path / "config-home"
    config_home.mkdir()
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))
    windows_claude = "/mnt/c/Users/example/AppData/Roaming/npm/claude.cmd"
    monkeypatch.setenv("OMNIGENT_CLAUDE_PATH", windows_claude)
    # The override is an absolute path; resolution passes it through
    # rather than looking up the bare "claude" name.
    monkeypatch.setattr(
        "omnigent._platform.shutil.which",
        lambda name: windows_claude if name == windows_claude else None,
    )

    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.forwarder.supervise_forwarder",
        _no_op_forwarder,
    )

    class _UnreachedResourceRegistry:
        """Fails the test if the launch reaches actual terminal spawn."""

        terminal_registry = None

        async def launch_required_terminal(self, **kwargs: Any) -> SessionResourceView:
            raise AssertionError(
                "terminal launch must not proceed past the interpreter-mismatch check"
            )

    def _handle_request(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"labels": {}})

    fake_client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(_handle_request),
    )

    with pytest.raises(ClaudeNativeHookInterpreterMismatchError, match="Windows-native"):
        await _auto_create_claude_terminal(
            "conv_wsl_env_override_mismatch",
            _UnreachedResourceRegistry(),
            lambda _sid, _evt: None,
            server_client=fake_client,
        )

    await fake_client.aclose()


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_honors_compatible_claude_env_override_under_wsl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A WSL-compatible ``OMNIGENT_CLAUDE_PATH`` override launches even with bad PATH.

    The override wins command resolution, so a working WSL-native binary
    must not be blocked merely because the PATH fallback would have
    resolved a Windows-native ``claude`` -- the launched spec carries the
    override as its command.
    """
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge.is_wsl", lambda: True)
    config_home = tmp_path / "config-home"
    config_home.mkdir()
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))
    compatible_override = tmp_path / "claude"
    compatible_override.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    compatible_override.chmod(0o755)
    monkeypatch.setenv("OMNIGENT_CLAUDE_PATH", str(compatible_override))
    windows_claude = "/mnt/c/Users/example/AppData/Roaming/npm/claude.cmd"
    monkeypatch.setattr(
        "omnigent._platform.shutil.which",
        lambda name: (
            str(compatible_override)
            if name == str(compatible_override)
            else (windows_claude if name == "claude" else None)
        ),
    )

    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.forwarder.supervise_forwarder",
        _no_op_forwarder,
    )

    captured: dict[str, Any] = {}

    def _handle_request(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"labels": {}})

    fake_client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(_handle_request),
    )

    await _auto_create_claude_terminal(
        "conv_wsl_good_override",
        _RecordingClaudeRegistry(captured),
        lambda _sid, _evt: None,
        server_client=fake_client,
    )

    assert captured["spec"].command == str(compatible_override)

    await fake_client.aclose()


@pytest.mark.asyncio
async def test_claude_launch_metadata_envelope_never_calls_server() -> None:
    """Resume configuration comes entirely from a v2 envelope."""

    class _NoCallbackClient:
        async def get(self, path: str, **_kwargs: Any) -> Any:
            raise AssertionError(f"envelope path made legacy callback: {path}")

    envelope = RunnerSessionInitEnvelope.model_validate(
        {
            "protocol_version": 2,
            "server_version": "0.6.0.dev0",
            "session_id": "conv_resume",
            "agent_id": "agent_resume",
            "snapshot": {
                "created_at": 10,
                "updated_at": 11,
                "workspace": "/tmp/worktree",
                "reasoning_effort": "high",
                "model_override": "claude-opus-4-7",
                "terminal_launch_args": ["--verbose"],
                "external_session_id": "claude-session-id",
                "labels": {"omnigent.fork.carry_history": "1"},
            },
        }
    )

    metadata = await _load_claude_launch_metadata(
        server_client=_NoCallbackClient(),  # type: ignore[arg-type]
        session_id="conv_resume",
        session_init=envelope,
    )

    assert metadata.reasoning_effort == "high"
    assert metadata.model_override == "claude-opus-4-7"
    assert metadata.terminal_launch_args == ["--verbose"]
    assert metadata.external_session_id == "claude-session-id"
    assert metadata.fork_carry_history is True


def test_agent_os_env_from_spec_unwraps_resolved_and_handles_none() -> None:
    """
    ``_agent_os_env_from_spec`` reads ``os_env`` through the resolved wrapper.

    The auto-create terminals receive either a bare ``AgentSpec`` or a
    ``ResolvedSpec`` wrapping one (the codex path passes ``ResolvedSpec``).
    The helper must unwrap the latter and return the inner ``os_env``, and
    return ``None`` when there is no spec — so the launch falls back to the
    platform default only when there is genuinely no agent policy to honour.
    """
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    os_env = OSEnvSpec(type="caller_process", cwd=".", sandbox=OSEnvSandboxSpec(type="none"))
    bare = AgentSpec(
        spec_version=1,
        name="agent",
        executor=ExecutorSpec(type="omnigent", config={}),
        os_env=os_env,
    )

    # Bare AgentSpec: returned directly.
    assert _agent_os_env_from_spec(bare) is os_env
    # ResolvedSpec wrapper: unwrapped to the inner spec's os_env.
    assert _agent_os_env_from_spec(ResolvedSpec(spec=bare, workdir=None)) is os_env
    # No spec at all: None (caller then uses the platform default).
    assert _agent_os_env_from_spec(None) is None
    # AgentSpec without an os_env block: None.
    no_os_env = AgentSpec(
        spec_version=1,
        name="agent",
        executor=ExecutorSpec(type="omnigent", config={}),
    )
    assert _agent_os_env_from_spec(no_os_env) is None


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_passes_raw_instructions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Host-spawned launch emits ``--append-system-prompt`` with the agent's
    raw author instructions.

    The managed-host wiring in ``runner/native/orchestration.py``
    (``augment_claude_args(..., append_system_prompt=
    _native_startup_raw_instructions_from_spec(agent_spec))``), where
    ``AgentSpec.instructions`` can become unreachable by claude-native.
    Drives the real ``_auto_create_claude_terminal`` →
    ``augment_claude_args`` integration rather than the helpers in isolation.
    """
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")

    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.forwarder.supervise_forwarder",
        _no_op_forwarder,
    )

    captured: dict[str, Any] = {}

    class _FakeResourceRegistry:
        terminal_registry = None

        async def launch_required_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            del terminal_name, session_key
            captured["spec"] = spec
            return SessionResourceView(
                id="terminal_claude_main",
                type="terminal",
                session_id=session_id,
                name="claude:main",
                metadata={"terminal_name": "claude", "session_key": "main", "running": True},
            )

    def _handle_request(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"labels": {}})

    fake_client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(_handle_request),
    )

    session_id = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
    agent_spec = AgentSpec(
        spec_version=1,
        name="claude-agent",
        instructions="Be a concise, careful coding assistant.",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    await _auto_create_claude_terminal(
        session_id,
        _FakeResourceRegistry(),
        lambda _sid, _evt: None,
        server_client=fake_client,
        agent_spec=agent_spec,
    )

    args = captured["spec"].args
    assert "--append-system-prompt" in args, f"missing --append-system-prompt in {args!r}"
    idx = args.index("--append-system-prompt")
    assert args[idx + 1] == "Be a concise, careful coding assistant."


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_inherits_agent_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Host-spawned Claude terminal honours the agent's ``os_env.sandbox``.

    Regression for the sandbox-override bug: the auto-create path built a
    fresh ``TerminalEnvSpec`` whose ``os_env`` carried no ``sandbox`` and
    passed no ``parent_os_env``, so ``launch_terminal`` fell back to
    ``_default_sandbox_for_platform`` (``linux_bwrap`` / ``darwin_seatbelt``)
    and ignored the agent YAML. A claude-native agent that declares
    ``os_env.sandbox.type: none`` (e.g. Polly's ``claude_code`` worker,
    which relies on the outer container/VM as the boundary) was wrongly
    forced into bwrap, which then failed to start in a hardened container.

    Asserts the launched terminal spec's ``os_env.sandbox`` is the agent's
    ``none`` sandbox (not the platform default) and that the agent's
    ``os_env`` is threaded through as the launch ``parent_os_env`` so the
    rest of the policy (egress_rules / env_passthrough) is inherited too.

    :param tmp_path: Pytest-provided temporary directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")

    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.forwarder.supervise_forwarder",
        _no_op_forwarder,
    )

    captured: dict[str, Any] = {}

    class _FakeResourceRegistry:
        """Captures the launched terminal spec and parent os_env."""

        terminal_registry = None

        async def launch_required_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """Record the spec + parent_os_env and return a resource view."""
            del terminal_name, session_key
            captured["spec"] = spec
            captured["parent_os_env"] = parent_os_env
            return SessionResourceView(
                id="terminal_claude_main",
                type="terminal",
                session_id=session_id,
                name="claude:main",
                metadata={"terminal_name": "claude", "session_key": "main", "running": True},
            )

    fake_client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"labels": {}}),
        ),
    )

    # An agent that declares sandbox: none (runs unconfined; the outer
    # container/VM is the boundary) — exactly Polly's coding sub-agents.
    agent_os_env = OSEnvSpec(
        type="caller_process",
        cwd=".",
        sandbox=OSEnvSandboxSpec(type="none"),
    )
    agent_spec = AgentSpec(
        spec_version=1,
        name="claude_code",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "claude-native", "model": "claude-default"},
        ),
        os_env=agent_os_env,
    )

    await _auto_create_claude_terminal(
        "e27fc87ef2a8d798895ce8c1e66db82d",
        _FakeResourceRegistry(),
        lambda _sid, _evt: None,
        server_client=fake_client,
        agent_spec=agent_spec,
    )

    launched_sandbox = captured["spec"].os_env.sandbox
    assert launched_sandbox is not None, (
        "auto-create dropped the agent's sandbox; launch_terminal will fall "
        "back to _default_sandbox_for_platform (bwrap), overriding sandbox: none"
    )
    assert launched_sandbox.type == "none"
    # The whole os_env is threaded through as the inheritance parent.
    assert captured["parent_os_env"] is agent_os_env

    await fake_client.aclose()


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_injects_ucode_gateway_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Host-spawned launch injects the ucode Databricks gateway config.

    On the daemon / web-UI path the runner — not the CLI — launches
    Claude, so it must reproduce the gateway auth the CLI normally
    injects: the ``ANTHROPIC_BASE_URL`` env, the ``apiKeyHelper`` token
    command, and the gateway default model. The runner derives this from
    the user's provider config (here the legacy global ``auth:`` block —
    the ambient ``DATABRICKS_CONFIG_PROFILE`` env var deliberately no
    longer steers credentials). Without it, Claude would launch with
    empty env and no token and could not reach the Databricks model —
    the exact regression that blocked daemon-routing.

    :param tmp_path: Pytest-provided temporary directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.harnesses.claude_native.main import ClaudeNativeUcodeConfig

    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    # The supported credential source for a host-spawned runner: the
    # global config's ``auth:`` block (written by ``omnigent setup``),
    # isolated to a temp config home so the developer's real config
    # can't leak in.
    config_home = tmp_path / "config-home"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(
        "auth:\n  type: databricks\n  profile: test-profile\n"
    )
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))

    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.forwarder.supervise_forwarder",
        _no_op_forwarder,
    )

    gateway_env = {"ANTHROPIC_BASE_URL": "https://gw.example/anthropic"}
    ucode = ClaudeNativeUcodeConfig(
        env=dict(gateway_env),
        api_key_helper="printf %s sk-sentinel-do-not-use",
        model="databricks-claude-opus-4-7",
        routable_models=(
            "databricks-claude-opus-4-7",
            "databricks-claude-opus-4-8",
            "databricks-claude-sonnet-5",
        ),
        model_overrides={
            "claude-opus-4-7": "databricks-claude-opus-4-7",
            "claude-opus-4-8": "databricks-claude-opus-4-8",
            "claude-sonnet-5": "databricks-claude-sonnet-5",
        },
    )
    # The runner imports ``_ucode_config_for_profile`` from
    # ``omnigent.harnesses.claude_native.main`` per call, so patch it at the source.
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.main._ucode_config_for_profile",
        lambda profile, *, refresh_models=True: ucode,
    )

    captured: dict[str, Any] = {}

    fake_client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"labels": {}}),
        ),
    )
    recorded_configs: dict[str, ClaudeNativeUcodeConfig | None] = {}

    await _auto_create_claude_terminal(
        "13efa494411f3ae60211e6be5635062a",
        _RecordingClaudeRegistry(captured),
        lambda _sid, _evt: None,
        server_client=fake_client,
        record_launch_config=recorded_configs.__setitem__,
    )

    spec = captured["spec"]
    # The gateway env points ``claude`` at the Databricks gateway, and
    # ENABLE_TOOL_SEARCH forces Claude Code to defer MCP tool schemas
    # instead of loading all 200+ bridge tools into startup context.
    assert spec.env == {
        **gateway_env,
        "ENABLE_TOOL_SEARCH": "true",
        "CLAUDE_CODE_DISABLE_AGENT_VIEW": "1",
        "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1",
        "CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK": "1",
    }
    assert spec.command == "claude"
    # The gateway default model is applied (no per-session override here).
    assert "--model" in spec.args
    assert spec.args[spec.args.index("--model") + 1] == "databricks-claude-opus-4-7"
    # The apiKeyHelper is registered in private settings, not subprocess argv.
    assert all("sk-sentinel-do-not-use" not in arg for arg in spec.args)
    settings = _load_claude_invocation_settings(spec.args)
    assert settings["apiKeyHelper"] == "printf %s sk-sentinel-do-not-use"
    assert settings["modelOverrides"] == {
        "claude-opus-4-7": "databricks-claude-opus-4-7",
        "claude-opus-4-8": "databricks-claude-opus-4-8",
        "claude-sonnet-5": "databricks-claude-sonnet-5",
    }
    assert recorded_configs == {"13efa494411f3ae60211e6be5635062a": ucode}

    await fake_client.aclose()


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_does_not_cache_transient_resolver_failure() -> None:
    """
    Stale-negative-cache scenario: a ``resolve_launch_config``
    exception used to still be recorded via ``record_launch_config(session_id,
    None)`` — indistinguishable, on the next
    ``_resolve_session_claude_launch_config`` read, from "this agent
    legitimately has no provider config" (presence-in-dict is the read
    gate). A transient failure (secret momentarily unavailable, a network
    blip resolving the ucode profile) would then permanently deny the
    session a provider-backed Claude terminal for the rest of the process
    lifetime. A resolver exception leaves the cache UNSET,
    so a later successful resolution still gets recorded.
    """
    from omnigent.harnesses.claude_native.main import ClaudeNativeUcodeConfig

    class _FakeResourceRegistry:
        """Records the launched terminal spec; returns a stub view."""

        terminal_registry = None

        async def launch_required_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            del terminal_name, session_key, spec, resource_role, parent_os_env
            return SessionResourceView(
                id="terminal_claude_main",
                type="terminal",
                session_id=session_id,
                name="claude:main",
                metadata={"terminal_name": "claude", "session_key": "main", "running": True},
            )

    fake_client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"labels": {}}),
        ),
    )
    session_id = "conv_launch_config_retry"
    recorded_configs: dict[str, ClaudeNativeUcodeConfig | None] = {}

    async def _failing_resolve_launch_config() -> ClaudeNativeUcodeConfig | None:
        raise RuntimeError("transient: secret temporarily unavailable")

    await _auto_create_claude_terminal(
        session_id,
        _FakeResourceRegistry(),
        lambda _sid, _evt: None,
        server_client=fake_client,
        resolve_launch_config=_failing_resolve_launch_config,
        record_launch_config=recorded_configs.__setitem__,
    )

    assert session_id not in recorded_configs, (
        f"A transient resolver failure must leave the launch-config cache "
        f"UNSET so the next attempt re-resolves — got "
        f"recorded_configs={recorded_configs!r}. Recording ``None`` here "
        f"would permanently deny this session a provider-backed Claude "
        f"terminal."
    )

    real_config = ClaudeNativeUcodeConfig(
        env={"ANTHROPIC_BASE_URL": "https://gw.example/anthropic"},
        api_key_helper="databricks auth token --fake-helper",
        model="databricks-claude-opus-4-7",
    )

    async def _succeeding_resolve_launch_config() -> ClaudeNativeUcodeConfig | None:
        return real_config

    await _auto_create_claude_terminal(
        session_id,
        _FakeResourceRegistry(),
        lambda _sid, _evt: None,
        server_client=fake_client,
        resolve_launch_config=_succeeding_resolve_launch_config,
        record_launch_config=recorded_configs.__setitem__,
    )

    assert recorded_configs == {session_id: real_config}, (
        f"A later successful resolution must be recorded — got "
        f"recorded_configs={recorded_configs!r}."
    )

    await fake_client.aclose()


async def _run_auto_create_cursor_terminal(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_spec: AgentSpec | None,
    terminal_launch_args: list[str] | None,
) -> dict[str, Any]:
    """Drive ``_auto_create_cursor_terminal`` and return what it wired up.

    Stubs the cursor-agent binary lookup, the transcript forwarder, and the
    runner auth factory so the model-injection branch runs without a real
    ``cursor-agent`` install or Databricks credentials. The session snapshot
    (workspace + ``terminal_launch_args``) is served from an in-memory
    ``httpx.MockTransport``, and a fake registry records the ``spec`` passed to
    ``launch_required_terminal`` so the test can assert on ``spec.args``.

    :returns: ``{"spec": <launch spec>, "elicitation_kwargs": <kwargs>}``, the
        latter captured from the elicitation supervisor the forwarder task
        gathers (empty if that task never got to start it).
    """
    from omnigent.runner import _entry as _runner_entry

    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr(cursor_native_bridge, "_BRIDGE_ROOT", tmp_path / "cursor-bridge")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(
        "omnigent.harnesses.cursor_native.main.resolve_cursor_executable", lambda: "cursor-agent"
    )

    monkeypatch.setattr(
        "omnigent.harnesses.cursor_native.forwarder.supervise_cursor_forwarder",
        _no_op_forwarder,
    )
    monkeypatch.setattr(
        "omnigent.harnesses.cursor_native.usage.supervise_cursor_usage_forwarder",
        _no_op_forwarder,
    )
    # The forwarder is stubbed, so the auth it would carry is never used — keep
    # the factory from reaching for ambient Databricks credentials in tests.
    monkeypatch.setattr(_runner_entry, "_make_auth_token_factory", lambda *a, **k: None)

    captured: dict[str, Any] = {"elicitation_kwargs": {}}
    started = asyncio.Event()

    async def _capture_elicitation_supervisor(**kwargs: Any) -> None:
        captured["elicitation_kwargs"] = kwargs
        started.set()

    monkeypatch.setattr(
        "omnigent.harnesses.cursor_native.permissions.supervise_cursor_transcript_elicitations",
        _capture_elicitation_supervisor,
    )

    class _FakeResourceRegistry:
        """Captures the launched terminal spec; no real terminal registry."""

        terminal_registry = None

        async def launch_required_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """Record the spec and return a terminal resource view."""
            del terminal_name, session_key, resource_role
            captured["parent_os_env"] = parent_os_env
            captured["spec"] = spec
            return SessionResourceView(
                id="terminal_cursor_main",
                type="terminal",
                session_id=session_id,
                name="cursor:main",
                metadata={"terminal_name": "cursor", "session_key": "main", "running": True},
            )

    snapshot = {"workspace": str(workspace), "terminal_launch_args": terminal_launch_args}
    fake_client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=snapshot)),
    )
    try:
        await _auto_create_cursor_terminal(
            "c42dbcb16fd3a87ee8f5d1fe4cabfdf8",
            _FakeResourceRegistry(),  # type: ignore[arg-type]
            lambda _sid, _evt: None,
            server_client=fake_client,
            ensure_comment_relay=None,
            agent_spec=agent_spec,
        )
        # The bridge supervisors are gathered in a background task, so give it
        # a beat to record the kwargs the wiring derived.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(started.wait(), timeout=2.0)
    finally:
        await fake_client.aclose()
    return captured


@pytest.mark.asyncio
async def test_auto_create_cursor_terminal_injects_spec_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spec-pinned cursor model is threaded into the cursor-agent launch args.

    The web-UI / daemon path launches ``cursor-agent`` from the runner, so the
    session's ``executor.model`` (from ``--model`` or config.yaml ``model:``)
    must reach the TUI as ``--model <id>`` — the regression #933 fixes.
    """
    captured = await _run_auto_create_cursor_terminal(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        agent_spec=AgentSpec(
            spec_version=1, name="cursor", executor=ExecutorSpec(model="sonnet-4-thinking")
        ),
        terminal_launch_args=None,
    )
    spec = captured["spec"]
    assert spec.command == "cursor-agent"
    assert "--model" in spec.args
    assert spec.args[spec.args.index("--model") + 1] == "sonnet-4-thinking"


@pytest.mark.asyncio
async def test_auto_create_cursor_terminal_inherits_agent_os_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cursor launches with the sandbox and environment declared by its agent."""
    agent_os_env = OSEnvSpec(sandbox=OSEnvSandboxSpec(type="none"))
    captured = await _run_auto_create_cursor_terminal(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        agent_spec=AgentSpec(spec_version=1, name="cursor", os_env=agent_os_env),
        terminal_launch_args=None,
    )

    assert captured["parent_os_env"] is agent_os_env
    assert captured["spec"].os_env.sandbox is agent_os_env.sandbox


@pytest.mark.parametrize(
    "passthrough",
    [
        # Both the split (``-- --model X``) and joined (``--model=X``) forms a
        # user can pass through must suppress injection — otherwise cursor-agent
        # sees two ``--model`` values and selection is ambiguous.
        ["--model", "gpt-5"],
        ["--model=gpt-5"],
        ["-m", "gpt-5"],
    ],
    ids=["split", "joined", "short"],
)
@pytest.mark.asyncio
async def test_auto_create_cursor_terminal_user_model_wins(
    passthrough: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user-pinned passthrough model wins; the spec model is not injected."""
    captured = await _run_auto_create_cursor_terminal(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        agent_spec=AgentSpec(
            spec_version=1, name="cursor", executor=ExecutorSpec(model="sonnet-4-thinking")
        ),
        terminal_launch_args=passthrough,
    )
    spec = captured["spec"]
    # Exactly the user's args survive — no second ``--model`` / spec model added.
    assert spec.args.count("--model") == passthrough.count("--model")
    assert "sonnet-4-thinking" not in spec.args


@pytest.mark.parametrize(
    "spec_model",
    [None, "", "databricks-claude-opus", "databricks/claude-opus"],
    ids=["none", "empty", "databricks-dash", "databricks-slash"],
)
@pytest.mark.asyncio
async def test_auto_create_cursor_terminal_omits_model_when_unusable(
    spec_model: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No usable cursor model id → no ``--model`` (cursor-agent keeps its default).

    Gateway-routed ``databricks-*`` ids are not cursor-agent model ids, so they
    are dropped rather than passed through (which would error on launch).
    """
    captured = await _run_auto_create_cursor_terminal(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        agent_spec=AgentSpec(
            spec_version=1, name="cursor", executor=ExecutorSpec(model=spec_model)
        ),
        terminal_launch_args=None,
    )
    assert "--model" not in captured["spec"].args


@pytest.mark.parametrize(
    ("terminal_launch_args", "expected"),
    [
        (None, False),
        (["--auto-review"], False),
        (["--yolo=false"], False),
        (["--yolo"], True),
        (["--force"], True),
        (["-f"], True),
    ],
    ids=["none", "auto-review", "yolo-off", "yolo", "force", "short-force"],
)
@pytest.mark.asyncio
async def test_auto_create_cursor_terminal_wires_yolo_auto_accept(
    terminal_launch_args: list[str] | None,
    expected: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The elicitation supervisor's yolo stance is derived from the launch args.

    This wiring is the only thing that turns the in-pane auto-accept on. It is
    a single kwarg in a large auto-create function, so without this assertion a
    rebase can drop it and leave the feature inert with the rest of the suite
    green.
    """
    captured = await _run_auto_create_cursor_terminal(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        agent_spec=None,
        terminal_launch_args=terminal_launch_args,
    )
    assert captured["elicitation_kwargs"]["auto_accept_approvals"] is expected


@pytest.mark.parametrize(
    (
        "snapshot_external_id",
        "fork_carry_history",
        "resume_transcript_available",
        "initial_prompt_recorded",
        "expected_start_at_end",
        "expected_initial_prompt_count",
    ),
    [
        ("02857840-6362-408f-b41f-309e396ed7c6", False, True, True, True, 0),
        ("02857840-6362-408f-b41f-309e396ed7c6", False, False, True, False, 0),
        (None, False, False, False, False, 1),
        (None, True, False, True, False, 0),
        ("02857840-6362-408f-b41f-309e396ed7c6", False, True, False, True, 1),
    ],
    ids=[
        "resume",
        "resume-without-transcript",
        "fresh",
        "history-carrying-fork-without-transcript",
        "older-client-explicit-resume-prompt",
    ],
)
@pytest.mark.asyncio
async def test_auto_create_claude_terminal_forwarder_skips_replayed_transcript_on_resume(
    snapshot_external_id: str | None,
    fork_carry_history: bool,
    resume_transcript_available: bool,
    initial_prompt_recorded: bool,
    expected_start_at_end: bool,
    expected_initial_prompt_count: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Host-spawned resume starts the forwarder past the replayed transcript.

    On cold resume the runner synthesizes Claude's local transcript from
    AP's committed history and launches ``claude --resume``, so the
    transcript file already holds every item Omnigent has at offset 0. The
    forwarder must therefore start at the transcript end
    (``start_at_end=True``); starting at offset 0 would re-post the whole
    history as new ``external_conversation_item`` records — which carry no
    server-side dedup — duplicating the visible conversation on every
    resume. A fresh session has no ``--resume`` and an empty
    transcript, so it must forward from the beginning
    (``start_at_end=False``). This mirrors the CLI client's
    ``prepared.cold_resumed`` handling in ``claude_native.py``.

    :param snapshot_external_id: ``external_session_id`` returned in the AP
        session snapshot, e.g.
        ``"02857840-6362-408f-b41f-309e396ed7c6"`` for a resume, or
        ``None`` for a fresh session.
    :param expected_start_at_end: The ``start_at_end`` the forwarder must
        be launched with for that snapshot.
    :param fork_carry_history: Whether the session is a history-carrying fork.
    :param resume_transcript_available: Whether prior history can be restored.
    :param initial_prompt_recorded: Whether history already holds the stored
        prompt; an older client's explicit resume prompt has not run yet.
    :param expected_initial_prompt_count: The stored prompt reaches the
        terminal once unless the session already ran it.
    :param tmp_path: Pytest-provided temporary directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path / "workspace"))
    (tmp_path / "workspace").mkdir()
    # Pin the launch config to Claude's native auth so the test does not
    # depend on the runner process's ambient Databricks profile.
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)

    forwarder_kwargs: dict[str, Any] = {}

    async def _capture_forwarder(**kwargs: Any) -> None:
        """Record the forwarder launch kwargs without opening a stream."""
        forwarder_kwargs.update(kwargs)

    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.forwarder.supervise_forwarder",
        _capture_forwarder,
    )

    # A missing transcript must not replay an already consumed initial prompt.
    synth_calls: list[str] = []

    async def _fake_synth(
        client: Any,
        *,
        session_id: str,
        external_session_id: str,
        workspace: Path,
        bridge_dir: Path,
        resume_copy: bool = False,
    ) -> Path | None:
        """Record the resume id and optionally provide a transcript."""
        assert not resume_copy
        assert bridge_dir == bridge_dir_for_bridge_id(session_id)
        del client, session_id, workspace
        synth_calls.append(external_session_id)
        if not resume_transcript_available:
            return None
        transcript_path = tmp_path / f"{external_session_id}.jsonl"
        transcript_path.touch()
        return transcript_path

    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.main._ensure_local_claude_resume_transcript",
        _fake_synth,
    )

    initial_prompt = "Reply with READY once.\nThen wait for a new instruction."
    snapshot: dict[str, Any] = {
        "terminal_launch_args": [initial_prompt, "--permission-mode", "auto", "--verbose"],
    }
    if snapshot_external_id is not None:
        snapshot["external_session_id"] = snapshot_external_id
    if fork_carry_history:
        snapshot["labels"] = {FORK_CARRY_HISTORY_LABEL_KEY: "1"}
    recorded_user_texts = [
        initial_prompt if initial_prompt_recorded else "An earlier instruction."
    ]

    launched_args: list[str] = []

    class _FakeResourceRegistry:
        """Resource registry that returns a terminal without launching."""

        terminal_registry = None

        async def launch_required_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """Return a terminal resource view without spawning a TTY."""
            del terminal_name, session_key
            launched_args.extend(spec.args)
            return SessionResourceView(
                id="terminal_claude_main",
                type="terminal",
                session_id=session_id,
                name="claude:main",
                metadata={"terminal_name": "claude", "session_key": "main", "running": True},
            )

    await _auto_create_claude_terminal(
        "5cdbea97a2fb0c659bc09605401e2bb2",
        _FakeResourceRegistry(),
        lambda _sid, _evt: None,
        server_client=_ResumeSnapshotServerClient(  # type: ignore[arg-type]
            "/v1/sessions/5cdbea97a2fb0c659bc09605401e2bb2",
            snapshot,
            items=[
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                }
                for text in recorded_user_texts
            ],
        ),
    )

    # The forwarder runs as a scheduled task; yield so the stub records its
    # kwargs before asserting.
    await asyncio.sleep(0)

    # Crux of the fix: resume skips the replayed transcript, a fresh session
    # forwards from the start. A regression to the old hardcoded
    # ``start_at_end=False`` flips the resume case False and reintroduces
    # the duplicate-history bug; the ``is`` comparison also fails if a
    # truthy non-bool leaks through.
    assert forwarder_kwargs.get("start_at_end") is expected_start_at_end, (
        f"forwarder start_at_end={forwarder_kwargs.get('start_at_end')!r}; "
        f"expected {expected_start_at_end!r} for external_session_id="
        f"{snapshot_external_id!r}. False on resume means the whole "
        f"transcript is re-posted."
    )

    # ``start_at_end`` must be correct *because* the resume branch ran, not
    # by coincidence: synthesis happens exactly when (and only when) the
    # snapshot carried an external session id.
    if snapshot_external_id is not None:
        assert synth_calls == [snapshot_external_id]
    else:
        assert len(synth_calls) == int(fork_carry_history)

    # Session titles are generated by an isolated background job, so neither
    # fresh nor resumed Claude terminals receive framework rename instructions.
    assert "--append-system-prompt" not in launched_args

    permission_mode_index = launched_args.index("--permission-mode")
    assert launched_args[permission_mode_index + 1] == "auto"
    assert "--verbose" in launched_args
    assert launched_args.count(initial_prompt) == expected_initial_prompt_count, (
        "The stored prompt must reach the terminal once and must not be "
        "submitted again after the session already ran it."
    )


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_resumes_a_copy_while_a_background_job_runs_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A cold resume of a session a background Claude job runs launches a copy.

    Claude refuses ``claude --resume <id>`` while its own background job still
    holds the session, so the Claude terminal would exit at startup. The
    runner must rebuild the history under a fresh id and resume that instead.
    """
    session_external_id = "02857840-6362-408f-b41f-309e396ed7c6"
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path / "workspace"))
    (tmp_path / "workspace").mkdir()
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    config_dir = tmp_path / "claude-config"
    (config_dir / "sessions").mkdir(parents=True)
    (config_dir / "sessions" / f"{os.getppid()}.json").write_text(
        json.dumps({"sessionId": session_external_id, "kind": "bg", "jobId": "ab12cd34"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))

    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.forwarder.supervise_forwarder",
        _no_op_forwarder,
    )
    synth_calls: list[str] = []

    async def _fake_synth(
        client: Any,
        *,
        session_id: str,
        external_session_id: str,
        workspace: Path,
        bridge_dir: Path,
        resume_copy: bool,
    ) -> Path:
        """Record the id the transcript is rebuilt under."""
        assert resume_copy
        del client, session_id, workspace, bridge_dir
        synth_calls.append(external_session_id)
        return tmp_path / f"{external_session_id}.jsonl"

    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.main._ensure_local_claude_resume_transcript",
        _fake_synth,
    )

    captured: dict[str, Any] = {}

    await _auto_create_claude_terminal(
        "5cdbea97a2fb0c659bc09605401e2bb2",
        _RecordingClaudeRegistry(captured),
        lambda _sid, _evt: None,
        server_client=_ResumeSnapshotServerClient(  # type: ignore[arg-type]
            "/v1/sessions/5cdbea97a2fb0c659bc09605401e2bb2",
            {"external_session_id": session_external_id},
        ),
    )

    launched_args = list(captured["spec"].args)
    assert len(synth_calls) == 1
    copy_session_id = synth_calls[0]
    assert copy_session_id != session_external_id
    resume_index = launched_args.index("--resume")
    assert launched_args[resume_index + 1] == copy_session_id
    assert session_external_id not in launched_args


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_resumes_in_place_when_the_registry_check_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registry probe that raises still resumes the session's own id, with its history."""
    session_external_id = "02857840-6362-408f-b41f-309e396ed7c6"
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path / "workspace"))
    (tmp_path / "workspace").mkdir()
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.forwarder.supervise_forwarder",
        _no_op_forwarder,
    )

    def broken_registry(session_id: str, **kwargs: object) -> None:
        raise RuntimeError("unreadable registry")

    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.main.find_background_holder", broken_registry
    )
    synth_calls: list[str] = []

    async def _fake_synth(
        client: Any,
        *,
        session_id: str,
        external_session_id: str,
        workspace: Path,
        bridge_dir: Path,
        resume_copy: bool,
    ) -> Path:
        """Record the id the transcript is rebuilt under."""
        assert not resume_copy
        del client, session_id, workspace, bridge_dir
        synth_calls.append(external_session_id)
        return tmp_path / f"{external_session_id}.jsonl"

    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.main._ensure_local_claude_resume_transcript",
        _fake_synth,
    )
    captured: dict[str, Any] = {}

    await _auto_create_claude_terminal(
        "5cdbea97a2fb0c659bc09605401e2bb2",
        _RecordingClaudeRegistry(captured),
        lambda _sid, _evt: None,
        server_client=_ResumeSnapshotServerClient(  # type: ignore[arg-type]
            "/v1/sessions/5cdbea97a2fb0c659bc09605401e2bb2",
            {"external_session_id": session_external_id},
        ),
    )

    launched_args = list(captured["spec"].args)
    assert synth_calls == [session_external_id]
    assert launched_args[launched_args.index("--resume") + 1] == session_external_id


@pytest.mark.asyncio
@pytest.mark.parametrize("source_transcript", ["missing", "unreadable", "available"])
async def test_auto_create_claude_fork_preserves_history_in_another_workspace(
    source_transcript: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fork rebuilds missing local history and resumes it in the chosen directory."""
    from unittest.mock import AsyncMock

    from omnigent.harnesses.claude_native import main as claude_native
    from omnigent.runner.native import orchestration
    from omnigent.runner.session_init_protocol import (
        SESSION_INIT_PROTOCOL_VERSION,
        RunnerSessionInitSnapshot,
    )

    session_id = "5cdbea97a2fb0c659bc09605401e2bb2"
    source_uuid = "11111111-1111-1111-1111-111111111111"
    workspace = tmp_path / "chosen-workspace"
    workspace.mkdir()
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "bridges")
    monkeypatch.setattr(
        claude_native_bridge, "ensure_claude_workspace_trusted", lambda *a, **k: None
    )
    monkeypatch.setattr(claude_native, "_CLAUDE_PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(orchestration, "resolve_cli_binary", lambda _: None)
    monkeypatch.setattr("omnigent.config.load_effective_config", lambda **_: {})
    forwarder = AsyncMock()
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.forwarder.supervise_forwarder", forwarder
    )

    source_path = tmp_path / "projects" / "old-workspace" / f"{source_uuid}.jsonl"
    source_text = json.dumps(
        {
            "type": "user",
            "uuid": "source-message",
            "parentUuid": None,
            "sessionId": source_uuid,
            "cwd": "/missing-workspace",
            "message": {"role": "user", "content": "Remember the recovery context."},
        }
    )
    if source_transcript != "missing":
        source_path.parent.mkdir(parents=True)
        source_path.write_text(source_text + "\n", encoding="utf-8")
    if source_transcript == "unreadable":

        def unreadable_clone(**_: Any) -> None:
            raise OSError("Source transcript is unreadable")

        monkeypatch.setattr(claude_native, "_clone_claude_transcript", unreadable_clone)

    history_requests: list[str] = []
    patched_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH":
            external_id = json.loads(request.content).get("external_session_id")
            if external_id:
                patched_ids.append(external_id)
            return httpx.Response(200, json={})
        assert request.url.path == f"/v1/sessions/{session_id}/items"
        history_requests.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "Remember the recovery context."}
                        ],
                    }
                ],
                "has_more": False,
            },
        )

    captured: dict[str, Any] = {}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8000"
    ) as client:
        await _auto_create_claude_terminal(
            session_id,
            _RecordingClaudeRegistry(captured),
            lambda _sid, _evt: None,
            server_client=client,
            session_init=RunnerSessionInitEnvelope(
                protocol_version=SESSION_INIT_PROTOCOL_VERSION,
                server_version="test",
                session_id=session_id,
                agent_id="agent",
                snapshot=RunnerSessionInitSnapshot(
                    created_at=0,
                    updated_at=0,
                    workspace=str(workspace),
                    labels={
                        "omnigent.fork.source_external_session_id": source_uuid,
                        "omnigent.fork.carry_history": "1",
                    },
                ),
            ),
            auth_token_factory=lambda: None,
            resolve_launch_config=AsyncMock(return_value=None),
        )
        await asyncio.sleep(0)

    spec = captured["spec"]
    assert spec.os_env.cwd == str(workspace)
    assert "--resume" in spec.args
    resume_id = spec.args[spec.args.index("--resume") + 1]
    assert resume_id != source_uuid
    assert patched_ids == [resume_id]
    transcript = claude_native._claude_project_dir_for_cwd(workspace) / f"{resume_id}.jsonl"
    records = [json.loads(line) for line in transcript.read_text(encoding="utf-8").splitlines()]
    assert records and all(record["cwd"] == str(workspace) for record in records)
    assert all(record["sessionId"] == resume_id for record in records)
    assert "Remember the recovery context." in transcript.read_text(encoding="utf-8")
    assert bool(history_requests) is (source_transcript != "available")
    assert forwarder.call_args.kwargs["start_at_end"] is True
    if source_transcript != "missing":
        assert source_path.read_text(encoding="utf-8") == source_text + "\n"


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_cold_resume_fallback_uses_pre_wipe_bridge_sid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fallback fires when server GET omits external_session_id but local bridge has it.

    Simulates the workspace-scope miss (ES-2065116): the server snapshot
    returns no external_session_id, but the bridge state.json from the
    previous launch holds a claude_session_id. The runner must read it
    *before* prepare_bridge_dir wipes the file and use it as the resume
    hint, so _ensure_local_claude_resume_transcript is called with the
    local sid and --resume is passed.
    """
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path / "workspace"))
    (tmp_path / "workspace").mkdir()
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)

    # Write the previous claude_session_id into the bridge state.json *before*
    # auto-create runs so the pre-wipe read can find it.
    session_id = "5cdbea97a2fb0c659bc09605401e2bb2"
    prior_claude_sid = "3d10247d-c3c0-4689-8cbd-862d7453bf70"
    pre_bridge_dir = bridge_dir_for_bridge_id(session_id)
    pre_bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    (pre_bridge_dir / "state.json").write_text(
        json.dumps({"claude_session_id": prior_claude_sid}), encoding="utf-8"
    )

    synth_calls: list[str] = []

    async def _fake_synth(
        client: Any,
        *,
        session_id: str,
        external_session_id: str,
        workspace: Path,
        bridge_dir: Path,
        resume_copy: bool,
    ) -> Path:
        assert bridge_dir == bridge_dir_for_bridge_id(session_id)
        assert not resume_copy
        del client, session_id, workspace
        synth_calls.append(external_session_id)
        return tmp_path / f"{external_session_id}.jsonl"

    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.main._ensure_local_claude_resume_transcript",
        _fake_synth,
    )

    forwarder_kwargs: dict[str, Any] = {}

    async def _capture_forwarder(**kwargs: Any) -> None:
        forwarder_kwargs.update(kwargs)

    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.forwarder.supervise_forwarder",
        _capture_forwarder,
    )

    class _NullBindingServerClient(NullServerClient):
        """Server client whose session snapshot omits external_session_id."""

        async def get(self, url: str, **kwargs: Any) -> NullServerClient._Response:
            del kwargs
            if url.endswith("/labels"):

                class _LabelsResponse(NullServerClient._Response):
                    def json(self) -> dict[str, Any]:
                        return {"labels": {}}

                return _LabelsResponse()

            # Session snapshot has no external_session_id (workspace-scope miss).
            class _SnapResponse(NullServerClient._Response):
                def json(self) -> dict[str, Any]:
                    return {}

            return _SnapResponse()

    class _FakeResourceRegistry:
        terminal_registry = None

        async def launch_required_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            del terminal_name, session_key, spec
            return SessionResourceView(
                id="terminal_claude_main",
                type="terminal",
                session_id=session_id,
                name="claude:main",
                metadata={"terminal_name": "claude", "session_key": "main", "running": True},
            )

    await _auto_create_claude_terminal(
        session_id,
        _FakeResourceRegistry(),
        lambda _sid, _evt: None,
        server_client=_NullBindingServerClient(),  # type: ignore[arg-type]
    )

    await asyncio.sleep(0)

    # The fallback must have fired: synthesis is called with the local bridge
    # sid, not skipped (which would leave the user with no context).
    assert synth_calls == [prior_claude_sid], (
        f"Expected synthesis with prior claude sid {prior_claude_sid!r}; "
        f"got {synth_calls!r}. The fallback may be reading the bridge dir "
        "after prepare_bridge_dir already wiped state.json."
    )
    # The forwarder must start past the replayed transcript (same as a
    # normal cold resume where the server returned the binding directly).
    assert forwarder_kwargs.get("start_at_end") is True


@dataclass
class _PublishedEvent:
    """
    One event captured from the runner's per-session publisher.

    :param session_id: Routing session id the event was published under,
        e.g. ``"c74c7a36c4736e2153ed6046d16bcf76"``.
    :param event: The published SSE event dict.
    """

    session_id: str
    event: dict[str, Any]


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_emits_resource_created_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Host-spawned terminal launch publishes a live ``session.resource.created``.

    The web UI sources its terminal list purely from SSE
    ``session.resource.created`` events, so the auto-create path must
    emit one (the agent-tool / REST launch paths already do via
    ``_emit_terminal_resource_event``). Without it the Terminal toggle
    stays gray until a refresh re-lists terminals via
    snapshot-on-connect.
    """
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")

    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.forwarder.supervise_forwarder",
        _no_op_forwarder,
    )

    class _ViewResourceRegistry:
        """Returns a terminal resource view; no live terminal registry."""

        # ``_publish_tmux_target_for_bridge`` early-returns when this is None.
        terminal_registry = None

        async def launch_required_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """Return the terminal resource view the runner would launch."""
            del spec
            return SessionResourceView(
                id="terminal_claude_main",
                type="terminal",
                session_id=session_id,
                name=f"{terminal_name}:{session_key}",
                metadata={
                    "terminal_name": terminal_name,
                    "session_key": session_key,
                    "running": True,
                },
            )

    published: list[_PublishedEvent] = []

    def _capture(session_id: str, event: dict[str, Any]) -> None:
        published.append(_PublishedEvent(session_id=session_id, event=event))

    await _auto_create_claude_terminal(
        "c74c7a36c4736e2153ed6046d16bcf76",
        _ViewResourceRegistry(),
        _capture,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    # Exactly one session.resource.created for the auto-created terminal.
    # 0 means the live publish was dropped (the bug: the toggle would stay
    # gray until a refresh triggers snapshot-on-connect).
    created = [p for p in published if p.event.get("type") == "session.resource.created"]
    assert len(created) == 1, (
        f"auto-create must publish exactly one session.resource.created; got {published}"
    )
    # Routed under the session id so the Omnigent relay forwards it to that
    # session's web stream.
    assert created[0].session_id == "c74c7a36c4736e2153ed6046d16bcf76"
    resource = created[0].event["resource"]
    assert resource["type"] == "terminal"
    assert resource["id"] == "terminal_claude_main"
    # metadata.running is what the web rail reads for the live terminal.
    assert resource["metadata"]["running"] is True


def test_publish_terminal_pending_emits_pending_then_clear() -> None:
    """
    ``_publish_terminal_pending`` emits the wire shape the Omnigent relay
    consumes for the Terminal-pill spinner.

    The session-creation handler calls this with ``True`` before
    auto-creating a terminal-first session's terminal and ``False`` in a
    ``finally`` (so a failed launch also clears the spinner). The AP
    relay matches on ``type == "session.terminal_pending"`` and reads
    the ``pending`` flag, so both fields must be present and correct, or
    the spinner would never appear (or never clear).
    """
    published: list[_PublishedEvent] = []

    def _capture(session_id: str, event: dict[str, Any]) -> None:
        published.append(_PublishedEvent(session_id=session_id, event=event))

    _publish_terminal_pending(_capture, "7cef62c6518d5591cc7991974e33ec4c", True)
    _publish_terminal_pending(_capture, "7cef62c6518d5591cc7991974e33ec4c", False)

    assert [p.event for p in published] == [
        {"type": "session.terminal_pending", "pending": True},
        {"type": "session.terminal_pending", "pending": False},
    ]
    # Routed under the session id so the Omnigent relay forwards it to that
    # session's web stream.
    assert all(p.session_id == "7cef62c6518d5591cc7991974e33ec4c" for p in published)


def test_publish_native_terminal_start_error_emits_failed_status_only(
    caplog: pytest.LogCaptureFixture,
    pinned_runner_log: Path,
) -> None:
    """
    Native terminal startup failure publishes a generic ``failed`` status.

    The runner must stay alive when terminal auto-create fails, but the
    affected session should only receive ``session.status: failed`` from
    this startup path. A bare ``response.error`` is turn-scoped; if the
    runner publishes one here, Omnigent can persist an orphan transcript error
    and then publish/persist a second error when the user message
    fast-fails against the same terminal.

    The published/returned message names the runner's log file — the raw
    exception text (which can embed paths/CLI details) is logged there for
    operators, not surfaced on the session stream.

    :param caplog: Pytest log capture fixture, used to confirm the raw
        cause is logged server-side.
    :param pinned_runner_log: The log path the message must name.
    """
    published: list[_PublishedEvent] = []

    def _capture(session_id: str, event: dict[str, Any]) -> None:
        published.append(_PublishedEvent(session_id=session_id, event=event))

    with caplog.at_level(logging.WARNING):
        error = _publish_native_terminal_start_error(
            _capture,
            "415c9954e2fe4b9276083a4d2c66f689",
            "Codex",
            ImportError("Native Codex requires the 'codex' CLI on PATH."),
        )

    # Client-safe payload pointing at the runner log — no raw exception text.
    error_id = error["error_id"]
    assert error_id.startswith("err_")
    assert len(error_id) == 36
    int(error_id.removeprefix("err_"), 16)
    assert error == {
        "code": "native_terminal_start_failed",
        "error_id": error_id,
        "message": (
            "Native Codex terminal failed to start (ImportError); "
            f"see the runner log for details: {pinned_runner_log} "
            f"Error ID: {error_id}."
        ),
    }
    # The raw cause must NOT leak into the surfaced message, but MUST be
    # logged for operators. If this fails, the redaction regressed (raw
    # text back in the payload) or the server-side log was dropped.
    assert "requires the 'codex' CLI" not in error["message"]
    # The structured, non-sensitive cause (exception type only, here) still
    # names the failure kind without the free-form message.
    assert "(ImportError)" in error["message"]
    assert "requires the 'codex' CLI on PATH." in caplog.text
    assert error_id in caplog.text
    assert [p.event for p in published] == [
        {
            "type": "session.status",
            "status": "failed",
            "error": error,
            "failure_context": {
                "failure_source": "native_terminal_start",
                "failure_id": error_id,
                "exception_type": "ImportError",
            },
        },
    ]
    assert all(p.session_id == "415c9954e2fe4b9276083a4d2c66f689" for p in published)


def test_publish_native_terminal_start_error_redacts_mismatch_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The actionable WSL mismatch response omits the rejected executable path."""
    published: list[_PublishedEvent] = []
    sensitive_path = "/mnt/c/Users/private/AppData/Roaming/npm/claude.cmd"

    def _capture(session_id: str, event: dict[str, Any]) -> None:
        published.append(_PublishedEvent(session_id=session_id, event=event))

    with caplog.at_level(logging.WARNING):
        error = _publish_native_terminal_start_error(
            _capture,
            "415c9954e2fe4b9276083a4d2c66f690",
            "Claude",
            ClaudeNativeHookInterpreterMismatchError(
                f"Claude Code executable {sensitive_path!r} is Windows-native"
            ),
        )

    error_id = error["error_id"]
    assert error_id.startswith("err_")
    assert len(error_id) == 36
    int(error_id.removeprefix("err_"), 16)
    assert error == {
        "code": "native_terminal_start_failed",
        "error_id": error_id,
        "message": (
            "Claude Code is Windows-native, but Omnigent is running under WSL. "
            "Install @anthropic-ai/claude-code from WSL so a WSL-native `claude` "
            f"binary wins PATH resolution, then retry. Error ID: {error_id}."
        ),
    }
    assert sensitive_path not in error["message"]
    assert sensitive_path in caplog.text
    assert error_id in caplog.text
    assert published[0].event["error"] == error


def test_terminal_lookup_miss_log_explains_stopped_registered_terminal(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Terminal GET miss logs identify a stopped registered terminal.

    The CLI polls ``GET /resources/terminals/terminal_claude_main`` while
    waiting to attach. If a tmux pane was registered and then failed the
    liveness probe, the runner log must say that instead of looking like
    auto-create never ran.

    :param tmp_path: Temporary directory for the fake tmux socket path.
    :param caplog: Pytest log capture fixture.
    :returns: None.
    """
    terminal_registry = TerminalRegistry()
    instance = TerminalInstance(
        name="claude",
        session_key="main",
        socket_path=tmp_path / "tmux.sock",
        private_dir=tmp_path,
        running=False,
    )
    terminal_registry._by_conversation["49b1b4ef0f1c9ba81d232a6f31dfeb24"] = {
        ("claude", "main"): instance
    }
    resource_registry = SessionResourceRegistry(terminal_registry=terminal_registry)

    _terminal_lookup_miss_log_state.clear()
    try:
        with caplog.at_level(logging.INFO, logger="omnigent.runner.app"):
            _log_terminal_lookup_miss(
                resource_registry,
                "49b1b4ef0f1c9ba81d232a6f31dfeb24",
                "terminal_claude_main",
            )
            _log_terminal_lookup_miss(
                resource_registry,
                "49b1b4ef0f1c9ba81d232a6f31dfeb24",
                "terminal_claude_main",
            )
    finally:
        _terminal_lookup_miss_log_state.clear()

    messages = [
        record.getMessage()
        for record in caplog.records
        if "Terminal resource lookup miss" in record.getMessage()
    ]
    assert len(messages) == 1, (
        f"lookup miss logging should be throttled per reason; got {messages!r}"
    )
    assert "terminal_registered_but_not_running" in messages[0]
    assert "terminal_claude_main" in messages[0]


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_resets_stale_bridge_id_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Auto-create corrects a stale ``bridge_id`` label on the Omnigent session.

    If a prior rotation left ``BRIDGE_ID_LABEL_KEY`` set to an older
    bridge id (e.g. ``"m0-bridge_from_prior_rotation"``),
    ``_auto_create_claude_terminal`` must PATCH the label to
    ``session_id`` before proceeding.  Without the correction,
    ``_ensure_comment_relay_started`` would later read the stale label
    and write ``tool_relay.json`` into the wrong bridge dir — the bridge
    MCP subprocess would never see it and the relay tools
    (``list_comments``, ``sys_session_list``, etc.) would be absent.
    """
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")

    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.forwarder.supervise_forwarder",
        _no_op_forwarder,
    )

    class _FakeResourceRegistry:
        """Returns a minimal terminal view; no live terminal registry."""

        terminal_registry = None

        async def launch_required_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """Return a minimal terminal view so the launch doesn't error."""
            del spec
            # Guards that _auto_create_claude_terminal tags the agent
            # terminal with the claude-native role — the runner gates
            # PTY-activity → session.status emission on this role, so
            # dropping it would silently disable working-status updates.
            assert resource_role == CLAUDE_NATIVE_TERMINAL_ROLE
            return SessionResourceView(
                id="terminal_claude_main",
                type="terminal",
                session_id=session_id,
                name=f"{terminal_name}:{session_key}",
                metadata={
                    "terminal_name": terminal_name,
                    "session_key": session_key,
                    "running": True,
                },
            )

    # Capture all HTTP requests made to the fake Omnigent server.
    recorded_requests: list[httpx.Request] = []

    def _handle(req: httpx.Request) -> httpx.Response:
        """Record every request; return 200 with minimal session payload."""
        recorded_requests.append(req)
        return httpx.Response(
            200,
            json={
                "reasoning_effort": None,
                "labels": {BRIDGE_ID_LABEL_KEY: "m0-bridge_from_prior_rotation"},
            },
            request=req,
        )

    fake_client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(_handle),
    )

    await _auto_create_claude_terminal(
        "9a0bec6675dc7ac693d7bf6f53cfb984",
        _FakeResourceRegistry(),
        lambda _sid, _evt: None,
        server_client=fake_client,
    )

    await fake_client.aclose()

    # Exactly one PATCH request must have been sent to correct the label.
    # 0 means the fix was not applied and the relay would target the wrong dir.
    patch_requests = [r for r in recorded_requests if r.method == "PATCH"]
    assert len(patch_requests) == 1, (
        f"Expected exactly one PATCH to correct the stale bridge_id label; "
        f"got {len(patch_requests)}. 0 means _auto_create_claude_terminal did "
        f"not update the label, so _ensure_comment_relay_started would write "
        f"tool_relay.json to a dir the bridge subprocess never reads."
    )

    import json as _json

    patch_body = _json.loads(patch_requests[0].content)
    assert (
        patch_body.get("labels", {}).get(BRIDGE_ID_LABEL_KEY) == "9a0bec6675dc7ac693d7bf6f53cfb984"
    ), (
        f"PATCH must set {BRIDGE_ID_LABEL_KEY!r} to the session_id "
        f"'9a0bec6675dc7ac693d7bf6f53cfb984' so _ensure_comment_relay_started finds the "
        f"correct bridge dir; got {patch_body.get('labels', {})!r}"
    )


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_honours_cleared_bridge_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A session re-keyed to "{id}-cleared" by /clear resumes in its OWN dir.

    The /clear rotation hands the live pane to the new session and re-keys the
    superseded session's bridge_id label to ``{session_id}-cleared``. When that
    session is later resumed, ``_auto_create_claude_terminal`` must honour the
    marker and prepare the isolated ``D({session_id}-cleared)`` — NOT the
    natural ``D(session_id)`` (the new session's live dir, which would
    double-mirror the transcript and trip the executor guard).
    """
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")

    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.forwarder.supervise_forwarder",
        _no_op_forwarder,
    )

    class _FakeInstance:
        """Minimal live terminal instance for the tmux-target publish."""

        running = True
        socket_path = "/tmp/fake-claude.sock"
        tmux_target = "claude:0.0"

    class _FakeTerminalRegistry:
        """Returns the live instance for any (session, terminal, key) lookup."""

        def get(self, conversation_id: str, terminal_name: str, session_key: str) -> Any:
            """Return the fake live instance."""
            del conversation_id, terminal_name, session_key
            return _FakeInstance()

    class _FakeResourceRegistry:
        """Resource registry exposing a live terminal registry."""

        terminal_registry = _FakeTerminalRegistry()

        async def launch_required_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """Return a minimal terminal view so the launch doesn't error."""
            del spec, resource_role
            return SessionResourceView(
                id="terminal_claude_main",
                type="terminal",
                session_id=session_id,
                name=f"{terminal_name}:{session_key}",
                metadata={
                    "terminal_name": terminal_name,
                    "session_key": session_key,
                    "running": True,
                },
            )

    recorded_requests: list[httpx.Request] = []

    def _handle(req: httpx.Request) -> httpx.Response:
        """Report the session's bridge_id label as the cleared marker."""
        recorded_requests.append(req)
        return httpx.Response(
            200,
            json={
                "reasoning_effort": None,
                "labels": {BRIDGE_ID_LABEL_KEY: "b3e788af0ecd4516439ee859b8c74536-cleared"},
            },
            request=req,
        )

    fake_client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(_handle),
    )

    await _auto_create_claude_terminal(
        "b3e788af0ecd4516439ee859b8c74536",
        _FakeResourceRegistry(),
        lambda _sid, _evt: None,
        server_client=fake_client,
    )
    await fake_client.aclose()

    cleared_dir = claude_native_bridge.bridge_dir_for_bridge_id(
        "b3e788af0ecd4516439ee859b8c74536-cleared"
    )
    natural_dir = claude_native_bridge.bridge_dir_for_bridge_id("b3e788af0ecd4516439ee859b8c74536")
    # The isolated cleared dir is prepared; the natural (live-sibling) dir is not.
    assert cleared_dir.exists()
    assert not natural_dir.exists()
    # tmux.json (what the executor reads to inject) must land in the SAME dir the
    # executor + forwarder use — the cleared dir — NOT the natural session_id dir.
    # Hardcoding session_id there was the "tmux target not advertised yet" bug.
    assert (cleared_dir / "tmux.json").exists()
    assert not (natural_dir / "tmux.json").exists()

    import json as _json

    patch_requests = [r for r in recorded_requests if r.method == "PATCH"]
    assert len(patch_requests) == 1
    patch_body = _json.loads(patch_requests[0].content)
    assert (
        patch_body.get("labels", {}).get(BRIDGE_ID_LABEL_KEY)
        == "b3e788af0ecd4516439ee859b8c74536-cleared"
    )


_RELAUNCHED_SESSION_ID = "6fe4feaa4dc772577c234c9f48c30dda"
_SOURCE_BRIDGE_ID = "971d4249f0c874d2718c0ea35ed53ef5"


class _LiveClaudeInstance:
    """Minimal live terminal instance for the tmux-target publish."""

    running = True
    socket_path = "/tmp/fake-claude.sock"
    tmux_target = "main"


class _LiveClaudeTerminalRegistry:
    """Returns the live instance for any (session, terminal, key) lookup."""

    def get(self, conversation_id: str, terminal_name: str, session_key: str) -> Any:
        """Return the fake live instance."""
        del conversation_id, terminal_name, session_key
        return _LiveClaudeInstance()


class _LiveClaudeResourceRegistry:
    """Resource registry whose launched Claude pane is live."""

    terminal_registry = _LiveClaudeTerminalRegistry()

    async def launch_required_terminal(
        self,
        *,
        session_id: str,
        terminal_name: str,
        session_key: str,
        spec: Any,
        resource_role: str | None = None,
        parent_os_env: Any = None,
    ) -> SessionResourceView:
        """Return a running terminal view."""
        del spec, resource_role, parent_os_env
        return SessionResourceView(
            id="terminal_claude_main",
            type="terminal",
            session_id=session_id,
            name=f"{terminal_name}:{session_key}",
            metadata={"terminal_name": terminal_name, "session_key": session_key, "running": True},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bridge_id_label", "moves"),
    [(_SOURCE_BRIDGE_ID, True), (_RELAUNCHED_SESSION_ID, False)],
    ids=["inherited_label", "own_label"],
)
async def test_auto_create_claude_terminal_rebinds_a_moved_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bridge_id_label: str,
    moves: bool,
) -> None:
    """
    A fresh pane for a session with an inherited bridge id moves it to its own dir.

    A /clear or /fork replacement carries its source's bridge id label. A new
    runner spawns the session's harness from that label, then launches the pane
    in ``D(session_id)``. The launch must persist the label, since turns after the
    init envelope expires read it from the server, and pass the move to
    ``rebind_bridge`` so the harness is respawned in the new dir. A session
    already on its own bridge id makes neither call.
    """
    from unittest.mock import AsyncMock

    from omnigent.runner.session_init_protocol import (
        SESSION_INIT_PROTOCOL_VERSION,
        RunnerSessionInitSnapshot,
    )

    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.forwarder.supervise_forwarder",
        _no_op_forwarder,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    envelope = RunnerSessionInitEnvelope(
        protocol_version=SESSION_INIT_PROTOCOL_VERSION,
        server_version="test",
        session_id=_RELAUNCHED_SESSION_ID,
        agent_id="agent",
        snapshot=RunnerSessionInitSnapshot(
            created_at=0,
            updated_at=0,
            workspace=str(workspace),
            labels={BRIDGE_ID_LABEL_KEY: bridge_id_label},
        ),
    )
    requests: list[httpx.Request] = []

    def _handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={}, request=request)

    rebinds: list[tuple[str, str]] = []

    async def _rebind(session_id: str, bridge_id: str) -> None:
        rebinds.append((session_id, bridge_id))

    async with httpx.AsyncClient(
        base_url="http://test-server", transport=httpx.MockTransport(_handle)
    ) as client:
        await _auto_create_claude_terminal(
            _RELAUNCHED_SESSION_ID,
            _LiveClaudeResourceRegistry(),
            lambda _sid, _evt: None,
            server_client=client,
            session_init=envelope,
            auth_token_factory=lambda: None,
            resolve_launch_config=AsyncMock(return_value=None),
            rebind_bridge=_rebind,
        )

    # The pane is advertised in the session's own dir either way.
    assert (bridge_dir_for_bridge_id(_RELAUNCHED_SESSION_ID) / "tmux.json").exists()
    assert envelope.snapshot.labels[BRIDGE_ID_LABEL_KEY] == _RELAUNCHED_SESSION_ID
    label_patches = [
        body["labels"]
        for request in requests
        if request.method == "PATCH"
        and BRIDGE_ID_LABEL_KEY in (body := json.loads(request.content)).get("labels", {})
    ]
    if moves:
        assert label_patches == [{BRIDGE_ID_LABEL_KEY: _RELAUNCHED_SESSION_ID}]
        assert rebinds == [(_RELAUNCHED_SESSION_ID, _RELAUNCHED_SESSION_ID)]
    else:
        assert label_patches == []
        assert rebinds == []


@pytest.mark.asyncio
@pytest.mark.parametrize("materialized_bundle", [False, True])
async def test_claude_skill_bundle_survives_retries_and_transfer_until_bridge_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    materialized_bundle: bool,
) -> None:
    """Generated skills share the bridge lifetime; supplied bundles remain caller-owned."""
    isolate_provider_resolution(monkeypatch, tmp_path / "config-home")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "bridges")
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.forwarder.supervise_forwarder",
        _no_op_forwarder,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    user_file = workspace / "notes.txt"
    user_file.write_text("Keep this workspace data.")
    supplied_bundle = tmp_path / "agent-bundle"
    supplied_bundle.mkdir()
    supplied_skill = supplied_bundle / "skills" / "example" / "SKILL.md"
    supplied_skill.parent.mkdir(parents=True)
    supplied_skill.write_text("---\nname: example\ndescription: Example\n---\nExample skill.")
    spec = AgentSpec(
        spec_version=1,
        name="example",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
        skills_filter="none",
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> ResolvedSpec:
        del agent_id, session_id
        return ResolvedSpec(spec=spec, workdir=supplied_bundle if materialized_bundle else None)

    terminal_registry = TerminalRegistry()
    resource_registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    bundles: list[Path] = []

    async def _launch(**kwargs: Any) -> SessionResourceView:
        args = kwargs["spec"].args
        bundle = Path(args[args.index("--plugin-dir") + 1])
        bundles.append(bundle)
        assert (bundle / "skills" / "build-omnigent" / "SKILL.md").is_file()
        assert args[args.index("--setting-sources") + 1] == ""
        assert "PermissionRequest" in _load_claude_invocation_settings(args)["hooks"]
        if len(bundles) == 1:
            raise OSError(errno.ENOSPC, "Synthetic terminal startup failure")
        return SessionResourceView(
            id="terminal_claude_main",
            type="terminal",
            session_id=kwargs["session_id"],
            name="claude:main",
            metadata={"terminal_name": "claude", "session_key": "main", "running": True},
        )

    monkeypatch.setattr(resource_registry, "launch_required_terminal", _launch)
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        resource_registry=resource_registry,
        auth_token_factory=lambda: None,
    )
    session_id = "skill-lifetime-source"
    async with _runner_client(app) as client:
        for attempt in range(3):
            response = await client.post(
                "/v1/sessions",
                json={
                    "session_id": session_id,
                    "agent_id": "example-agent",
                    "session_init": {
                        "protocol_version": 2,
                        "server_version": "test",
                        "session_id": session_id,
                        "agent_id": "example-agent",
                        "snapshot": {
                            "created_at": 0,
                            "updated_at": 0,
                            "workspace": str(workspace),
                            "labels": {},
                        },
                    },
                },
            )
            assert response.status_code == 201, response.text
            assert response.json()["terminal_ready"] is (attempt > 0)

    assert len(bundles) == 3
    assert len(set(bundles)) == 1, "launch retries must not leave unowned skill bundles"
    bundle = bundles[0]
    bridge_dir = bridge_dir_for_bridge_id(session_id)
    assert bundle == supplied_bundle if materialized_bundle else bundle.is_relative_to(bridge_dir)

    instance = make_test_terminal_instance("claude", "main", tmp_path)
    terminal_registry._by_conversation[session_id] = {("claude", "main"): instance}
    moved = await resource_registry.transfer_terminal(
        session_id, "skill-lifetime-target", "terminal_claude_main"
    )
    assert moved is not None
    await resource_registry.cleanup_session(session_id)
    assert claude_native_bridge.prune_orphaned_bridge_dirs() == 0
    assert (bundle / "skills" / "build-omnigent" / "SKILL.md").is_file()

    instance.running = False
    monkeypatch.setattr("omnigent.inner.terminal._process_alive", lambda _pid: False)
    assert claude_native_bridge.prune_orphaned_bridge_dirs() == 1
    assert not bridge_dir.exists()
    assert bundle.exists() is materialized_bundle
    assert supplied_skill.is_file()
    assert user_file.read_text() == "Keep this workspace data."


@pytest.mark.asyncio
async def test_relaunch_that_moves_the_bridge_respawns_the_harness_there(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The turn after a relaunch moved the Claude bridge spawns its harness in the new dir.

    Session init spawns the harness from the inherited bridge id label before the
    terminal launch moves the bridge. The runner's ``rebind_bridge`` must release
    that harness and update the cached init labels, so the next turn's spawn env
    names the dir the pane advertises in. Before, every web message waited 30 s
    in the old dir and failed with "tmux target is not advertised yet".
    """
    from omnigent.harnesses.claude_native.bridge import BRIDGE_DIR_ENV_VAR

    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    agent_id = "b1428afa05a7169b76a73ae805a6f71f"
    launches: list[str] = []

    async def _moving_auto_create(
        session_id: str,
        resource_registry: Any,
        publish_event: Any,
        **kwargs: Any,
    ) -> SessionResourceView:
        # The real launch moves a session off an inherited bridge id this way.
        del resource_registry, publish_event
        launches.append(session_id)
        if len(launches) == 1:
            await kwargs["rebind_bridge"](session_id, session_id)
        return SessionResourceView(
            id="terminal_claude_main",
            type="terminal",
            session_id=session_id,
            name="claude:main",
            metadata={"terminal_name": "claude", "session_key": "main", "running": True},
        )

    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._auto_create_claude_terminal",
        _moving_auto_create,
    )
    spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=TerminalRegistry(),
    )

    async with _runner_client(app) as client:
        created = await client.post(
            "/v1/sessions",
            json={
                "session_id": _RELAUNCHED_SESSION_ID,
                "agent_id": agent_id,
                "session_init": {
                    "protocol_version": 2,
                    "server_version": "0.19.0.dev0",
                    "session_id": _RELAUNCHED_SESSION_ID,
                    "agent_id": agent_id,
                    "snapshot": {
                        "created_at": 10,
                        "updated_at": 11,
                        "workspace": str(tmp_path),
                        "labels": {BRIDGE_ID_LABEL_KEY: _SOURCE_BRIDGE_ID},
                    },
                },
            },
        )
        assert created.status_code == 201, created.text
        # Init spawned the harness from the inherited label, then the launch
        # released it.
        assert pm.get_client_calls[0][2][BRIDGE_DIR_ENV_VAR] == str(
            bridge_dir_for_bridge_id(_SOURCE_BRIDGE_ID)
        )
        assert pm.released == [_RELAUNCHED_SESSION_ID]

        sent = await client.post(
            f"/v1/sessions/{_RELAUNCHED_SESSION_ID}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": agent_id,
                "content": [{"type": "input_text", "text": "hello"}],
            },
        )
        assert sent.status_code == 202, sent.text
        for _ in range(100):
            if len(pm.get_client_calls) > 1:
                break
            await asyncio.sleep(0.02)

    turn_spawns = [
        env for _conv, harness, env in pm.get_client_calls[1:] if harness == "claude-native"
    ]
    assert turn_spawns, f"the turn never acquired its harness: {pm.get_client_calls!r}"
    assert turn_spawns[0][BRIDGE_DIR_ENV_VAR] == str(
        bridge_dir_for_bridge_id(_RELAUNCHED_SESSION_ID)
    )


@dataclass
class _AutoCreateScenario:
    """
    One parametrized case for the claude-native auto-create guard.

    :param case_id: Human-readable scenario id used as the pytest id,
        e.g. ``"clear_rotation_target_skips"``.
    :param active_session_id: ``active_session_id`` to seed into the
        shared bridge config, e.g. ``"3bb59abc6e20b834cbb2269f28880895"``. ``None`` seeds no
        bridge dir at all (models a genuinely fresh session).
    :param terminal_under: Session id to seed a live ``claude:main``
        terminal under in the registry, e.g. ``"3bb59abc6e20b834cbb2269f28880895"``. ``None``
        seeds no terminal (models a dead/absent original terminal).
    :param bridge_id_label: Value returned for the new session's
        ``BRIDGE_ID_LABEL_KEY`` label, e.g. ``"bridge_shared"`` for a
        rotation target (shares the original's bridge) or ``"2d1b1a96e3e08f2cd43c0cc4b695ac5d"``
        for a fresh session (own bridge).
    :param expect_auto_create: Whether the guard should invoke
        ``_auto_create_claude_terminal`` for the new session.
    """

    case_id: str
    active_session_id: str | None
    terminal_under: str | None
    bridge_id_label: str
    expect_auto_create: bool


class _LabelsAndEmptyHistoryServerClient:
    """
    Server-client stub for the auto-create guard route test.

    Answers the two GETs ``create_session`` issues for a claude-native
    session: the session snapshot (returns a ``BRIDGE_ID_LABEL_KEY``
    label so the guard can resolve the bridge id) and the items page
    (returns empty history so no crash-recovery turn starts). A real
    stub class — not ``MagicMock`` — so an unexpected call shape fails
    loudly instead of silently returning a mock.
    """

    def __init__(self, bridge_id_label: str) -> None:
        """
        :param bridge_id_label: Bridge id to report on the session's
            ``labels``, e.g. ``"bridge_shared"``.
        """
        self._bridge_id_label = bridge_id_label

    async def get(self, url: str, **kwargs: Any) -> Any:
        """
        Return a canned snapshot or empty items page for *url*.

        :param url: Request path, e.g. ``"/v1/sessions/2d1b1a96e3e08f2cd43c0cc4b695ac5d"`` or
            ``"/v1/sessions/2d1b1a96e3e08f2cd43c0cc4b695ac5d/items"``.
        :returns: A response object exposing ``status_code`` and
            ``json()`` matching the subset the runner reads.
        """
        del kwargs

        class _Response:
            """Minimal httpx-like response with the fields the runner reads."""

            def __init__(self, payload: dict[str, Any]) -> None:
                """:param payload: JSON body returned by ``json()``."""
                self.status_code = 200
                self._payload = payload

            def json(self) -> dict[str, Any]:
                """:returns: The canned JSON payload."""
                return self._payload

        if url.endswith("/items"):
            return _Response({"data": [], "has_more": False})
        return _Response({"labels": {BRIDGE_ID_LABEL_KEY: self._bridge_id_label}})


_AUTO_CREATE_SCENARIOS = [
    # Rotation target: the bridge's active session (conv_old) still owns
    # the live terminal that is about to be transferred onto conv_new.
    _AutoCreateScenario(
        case_id="clear_rotation_target_skips",
        active_session_id="3bb59abc6e20b834cbb2269f28880895",
        terminal_under="3bb59abc6e20b834cbb2269f28880895",
        bridge_id_label="bridge_shared",
        expect_auto_create=False,
    ),
    # Fresh host-spawned session: its own bridge has no recorded active
    # session and no terminal, so it must bootstrap its own Claude.
    _AutoCreateScenario(
        case_id="fresh_session_creates",
        active_session_id=None,
        terminal_under=None,
        bridge_id_label="2d1b1a96e3e08f2cd43c0cc4b695ac5d",
        expect_auto_create=True,
    ),
    # The bridge's active session is conv_new itself (e.g. a relaunch
    # after the terminal died) — not a rotation, so auto-create proceeds.
    _AutoCreateScenario(
        case_id="active_is_self_creates",
        active_session_id="2d1b1a96e3e08f2cd43c0cc4b695ac5d",
        terminal_under=None,
        bridge_id_label="bridge_shared",
        expect_auto_create=True,
    ),
    # The bridge names an active sibling (conv_old) but no live terminal
    # exists under it — nothing to transfer in, so auto-create proceeds.
    _AutoCreateScenario(
        case_id="dead_terminal_under_active_creates",
        active_session_id="3bb59abc6e20b834cbb2269f28880895",
        terminal_under=None,
        bridge_id_label="bridge_shared",
        expect_auto_create=True,
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario", _AUTO_CREATE_SCENARIOS, ids=[s.case_id for s in _AUTO_CREATE_SCENARIOS]
)
@pytest.mark.parametrize("use_envelope", [False, True], ids=["legacy", "envelope"])
async def test_create_session_auto_create_guard_skips_rotation_targets(
    scenario: _AutoCreateScenario,
    use_envelope: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The claude-native auto-create guard skips ``/clear`` rotation targets.

    A ``/clear`` or ``/fork`` rotation binds the runner to a fresh AP
    session, then transfers the existing Claude terminal onto it. The
    bind reaches the runner's ``POST /v1/sessions`` before the transfer
    runs, so the new session momentarily has no terminal. Previously,
    ``create_session`` always auto-created a second Claude here, which
    made the subsequent transfer 409 and looped the rotation into
    unbounded session/process spawning. The guard now skips auto-create
    when the new session's bridge already has a *different* session
    owning a live ``claude:main`` terminal — the one about to be
    transferred in.

    Drives the real route with the real guard. Each scenario seeds the
    shared bridge's ``active_session_id`` and the terminal registry, then
    asserts whether ``_auto_create_claude_terminal`` ran. Reverting the
    guard turns the ``clear_rotation_target_skips`` case red (auto-create
    fires for a rotation target again).
    """
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")

    # Seed the shared bridge config so the guard reads the original
    # (terminal-owning) session as the bridge's active session.
    if scenario.active_session_id is not None:
        prepare_bridge_dir(
            scenario.active_session_id,
            bridge_id=scenario.bridge_id_label,
            workspace=tmp_path,
        )

    # Seed a live claude:main terminal under the original session so the
    # guard's registry probe finds the terminal that would be transferred.
    # Poking ``_by_conversation`` directly is the established registry-test
    # idiom (see tests/terminals/test_registry.py) — a real TerminalInstance
    # without launching tmux.
    terminal_registry = TerminalRegistry()
    if scenario.terminal_under is not None:
        instance = TerminalInstance(
            name="claude",
            session_key="main",
            socket_path=tmp_path / "claude.sock",
            private_dir=tmp_path / "claude",
            running=True,
        )
        terminal_registry._by_conversation[scenario.terminal_under] = {
            ("claude", "main"): instance
        }

    created: list[str] = []

    async def _recording_auto_create(
        session_id: str, resource_registry: Any, publish_event: Any, **_kwargs: Any
    ) -> None:
        """
        Record the auto-create call instead of launching a real Claude.

        :param session_id: Session id the guard chose to auto-create for,
            e.g. ``"2d1b1a96e3e08f2cd43c0cc4b695ac5d"``.
        :param resource_registry: Unused — the real launch path is stubbed.
        :param publish_event: Unused — the real launch path is stubbed.
        :param _kwargs: Absorbs keyword args added to the real function
            (e.g. ``server_client``).
        :returns: None.
        """
        del resource_registry, publish_event
        created.append(session_id)

    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._auto_create_claude_terminal", _recording_auto_create
    )

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """
        Return the claude-native spec for any agent id.

        :param agent_id: Requested agent id (unused — fixed spec).
        :param session_id: Requested session id (unused — fixed spec).
        :returns: The claude-native :class:`AgentSpec`.
        """
        del agent_id, session_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_LabelsAndEmptyHistoryServerClient(  # type: ignore[arg-type]
            scenario.bridge_id_label
        ),
        terminal_registry=terminal_registry,
    )

    payload: dict[str, Any] = {
        "session_id": "2d1b1a96e3e08f2cd43c0cc4b695ac5d",
        "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
    }
    if use_envelope:
        payload["session_init"] = {
            "protocol_version": 2,
            "server_version": "0.6.0.dev0",
            "session_id": payload["session_id"],
            "agent_id": payload["agent_id"],
            "snapshot": {
                "created_at": 10,
                "updated_at": 11,
                "workspace": str(tmp_path),
                "labels": {BRIDGE_ID_LABEL_KEY: scenario.bridge_id_label},
            },
        }

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json=payload,
        )
    assert resp.status_code == 201, resp.text

    if scenario.expect_auto_create:
        # Fresh / no-live-sibling sessions must still bootstrap their own
        # Claude — the guard only suppresses true rotation targets. An
        # empty ``created`` here would mean the guard over-fired and a
        # host-spawned session would never get a terminal.
        assert created == ["2d1b1a96e3e08f2cd43c0cc4b695ac5d"], (
            f"Expected auto-create for {scenario.case_id}; got {created}"
        )
    else:
        # The rotation target's terminal arrives via transfer. Auto-create
        # here is the regression: it 409s the transfer and loops the
        # rotation into unbounded session spawning.
        assert created == [], f"Auto-create must be skipped for {scenario.case_id}; got {created}"


@dataclass
class _AntigravityAutoCreateScenario:
    """
    One parametrized case for the antigravity-native auto-create guard.

    :param case_id: Human-readable scenario id used as the pytest id,
        e.g. ``"clear_rotation_target_skips"``.
    :param bridge_state_session: ``session_id`` to seed into the shared
        bridge state, e.g. ``"3bb59abc6e20b834cbb2269f28880895"``. ``None`` seeds no bridge state
        at all (models a genuinely fresh session).
    :param terminal_under: Session id to seed a live ``antigravity:main``
        terminal under in the registry, e.g. ``"3bb59abc6e20b834cbb2269f28880895"``. ``None``
        seeds no terminal (models a dead/absent original terminal).
    :param bridge_id_label: Value returned for the new session's
        :data:`ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY` label, e.g.
        ``"bridge_shared"`` for a rotation target (shares the original's
        bridge) or ``"2d1b1a96e3e08f2cd43c0cc4b695ac5d"`` for a fresh session (own bridge).
    :param expect_auto_create: Whether the guard should invoke
        ``_auto_create_antigravity_terminal`` for the new session.
    """

    case_id: str
    bridge_state_session: str | None
    terminal_under: str | None
    bridge_id_label: str
    expect_auto_create: bool


class _AntigravitySnapshotServerClient:
    """
    Server-client stub for the antigravity auto-create guard route test.

    Answers the two GETs the antigravity branch issues for that session: the
    session snapshot (``/v1/sessions/2d1b1a96e3e08f2cd43c0cc4b695ac5d`` — non-``None`` so
    ``_session_payload_for_host_spawn_check`` reports the session needs a
    terminal) and the labels lookup (``/v1/sessions/<id>/labels`` — returns
    the bridge-id label so the transfer-inbound check can resolve the shared
    bridge dir). A real stub class — not ``MagicMock`` — so an unexpected call
    shape fails loudly instead of silently returning a mock.
    """

    def __init__(self, bridge_id_label: str) -> None:
        """
        :param bridge_id_label: Bridge id to report on the session's
            ``labels``, e.g. ``"bridge_shared"``.
        """
        self._bridge_id_label = bridge_id_label

    async def get(self, url: str, **kwargs: Any) -> Any:
        """
        Return a canned snapshot or labels payload for *url*.

        :param url: Request path, e.g. ``"/v1/sessions/2d1b1a96e3e08f2cd43c0cc4b695ac5d"`` or
            ``"/v1/sessions/2d1b1a96e3e08f2cd43c0cc4b695ac5d/labels"``.
        :returns: A response object exposing ``status_code`` and ``json()``
            matching the subset the runner reads.
        """
        del kwargs
        labels = {ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY: self._bridge_id_label}

        class _Response:
            """Minimal httpx-like response with the fields the runner reads."""

            def __init__(self, payload: dict[str, Any]) -> None:
                """:param payload: JSON body returned by ``json()``."""
                self.status_code = 200
                self._payload = payload

            def json(self) -> dict[str, Any]:
                """:returns: The canned JSON payload."""
                return self._payload

        if url.endswith("/labels"):
            return _Response({"labels": labels})
        # The session snapshot: non-None so the host-spawn check reports the
        # session needs a terminal, and carries the same labels.
        return _Response({"id": "2d1b1a96e3e08f2cd43c0cc4b695ac5d", "labels": labels})


_ANTIGRAVITY_AUTO_CREATE_SCENARIOS = [
    # Rotation target: the bridge's active session (conv_old) still owns the
    # live agy terminal that is about to be transferred onto conv_new.
    _AntigravityAutoCreateScenario(
        case_id="clear_rotation_target_skips",
        bridge_state_session="3bb59abc6e20b834cbb2269f28880895",
        terminal_under="3bb59abc6e20b834cbb2269f28880895",
        bridge_id_label="bridge_shared",
        expect_auto_create=False,
    ),
    # Fresh host-spawned session: its own bridge has no recorded state and no
    # terminal, so it must bootstrap (cold-start) its own agy.
    _AntigravityAutoCreateScenario(
        case_id="fresh_session_creates",
        bridge_state_session=None,
        terminal_under=None,
        bridge_id_label="2d1b1a96e3e08f2cd43c0cc4b695ac5d",
        expect_auto_create=True,
    ),
    # The bridge's recorded session is conv_new itself (e.g. a relaunch after
    # the terminal died) — not a rotation, so auto-create proceeds.
    _AntigravityAutoCreateScenario(
        case_id="active_is_self_creates",
        bridge_state_session="2d1b1a96e3e08f2cd43c0cc4b695ac5d",
        terminal_under=None,
        bridge_id_label="bridge_shared",
        expect_auto_create=True,
    ),
    # The bridge names a sibling (conv_old) but no live terminal exists under
    # it — nothing to transfer in, so auto-create proceeds.
    _AntigravityAutoCreateScenario(
        case_id="dead_terminal_under_active_creates",
        bridge_state_session="3bb59abc6e20b834cbb2269f28880895",
        terminal_under=None,
        bridge_id_label="bridge_shared",
        expect_auto_create=True,
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    _ANTIGRAVITY_AUTO_CREATE_SCENARIOS,
    ids=[s.case_id for s in _ANTIGRAVITY_AUTO_CREATE_SCENARIOS],
)
async def test_create_session_antigravity_auto_create_guard_skips_rotation_targets(
    scenario: _AntigravityAutoCreateScenario,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The antigravity-native auto-create guard skips ``/clear`` rotation targets.

    A ``/clear`` rotation binds the runner to a fresh Omnigent session, then
    transfers the existing agy terminal onto it — agy is one long-lived process
    hosting many cascades, so the rotation re-homes the SAME process. The bind
    reaches the runner's ``POST /v1/sessions`` before the transfer runs, so the
    new session momentarily has no terminal. Auto-creating a second agy here
    cold-starts a brand-new process whose own ``external_session_id`` then 400s
    the rotation's PATCH and loops it into unbounded session/process spawning
    (the bug found by live e2e). The guard now skips auto-create when the new
    session's bridge already has a *different* session owning a live
    ``antigravity:main`` terminal — the one about to be transferred in. Mirrors
    the claude-native guard test above.

    Drives the real route with the real guard. Each scenario seeds the shared
    bridge state's ``session_id`` and the terminal registry, then asserts whether
    ``_auto_create_antigravity_terminal`` ran. Reverting the guard turns the
    ``clear_rotation_target_skips`` case red (auto-create fires for a rotation
    target again).
    """
    monkeypatch.setattr(
        "omnigent.harnesses.antigravity_native.bridge._BRIDGE_ROOT",
        tmp_path / "antigravity-native",
    )

    # Seed the shared bridge state so the guard reads the original
    # (terminal-owning) session as the bridge's active session.
    if scenario.bridge_state_session is not None:
        seed_dir = prepare_antigravity_bridge_dir(scenario.bridge_id_label)
        write_antigravity_bridge_state(
            seed_dir,
            AntigravityNativeBridgeState(
                session_id=scenario.bridge_state_session,
                conversation_id="cascade_old",
            ),
        )

    # Seed a live antigravity:main terminal under the original session so the
    # guard's registry probe finds the terminal that would be transferred.
    # Poking ``_by_conversation`` directly is the established registry-test
    # idiom — a real TerminalInstance without launching tmux.
    terminal_registry = TerminalRegistry()
    if scenario.terminal_under is not None:
        instance = TerminalInstance(
            name="antigravity",
            session_key="main",
            socket_path=tmp_path / "antigravity.sock",
            private_dir=tmp_path / "antigravity",
            running=True,
        )
        terminal_registry._by_conversation[scenario.terminal_under] = {
            ("antigravity", "main"): instance
        }

    created: list[str] = []

    async def _recording_auto_create(
        session_id: str, resource_registry: Any, publish_event: Any, **_kwargs: Any
    ) -> None:
        """
        Record the auto-create call instead of launching a real agy.

        :param session_id: Session id the guard chose to auto-create for,
            e.g. ``"2d1b1a96e3e08f2cd43c0cc4b695ac5d"``.
        :param resource_registry: Unused — the real launch path is stubbed.
        :param publish_event: Unused — the real launch path is stubbed.
        :param _kwargs: Absorbs keyword args added to the real function
            (e.g. ``server_client``).
        :returns: None.
        """
        del resource_registry, publish_event
        created.append(session_id)

    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._auto_create_antigravity_terminal",
        _recording_auto_create,
    )

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "antigravity-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """
        Return the antigravity-native spec for any agent id.

        :param agent_id: Requested agent id (unused — fixed spec).
        :param session_id: Requested session id (unused — fixed spec).
        :returns: The antigravity-native :class:`AgentSpec`.
        """
        del agent_id, session_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_AntigravitySnapshotServerClient(  # type: ignore[arg-type]
            scenario.bridge_id_label
        ),
        terminal_registry=terminal_registry,
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "2d1b1a96e3e08f2cd43c0cc4b695ac5d",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
    assert resp.status_code == 201, resp.text

    if scenario.expect_auto_create:
        # Fresh / no-live-sibling sessions must still bootstrap their own agy —
        # the guard only suppresses true rotation targets. An empty ``created``
        # here would mean the guard over-fired and a host-spawned session would
        # never get a terminal.
        assert created == ["2d1b1a96e3e08f2cd43c0cc4b695ac5d"], (
            f"Expected auto-create for {scenario.case_id}; got {created}"
        )
    else:
        # The rotation target's terminal arrives via transfer. Auto-create here
        # is the regression: it cold-starts a redundant agy that 400s the
        # rotation's external_session_id PATCH and loops the rotation.
        assert created == [], f"Auto-create must be skipped for {scenario.case_id}; got {created}"


@dataclass
class _CodexAutoCreateScenario:
    """
    One case for the codex-native auto-create guard route test.

    :param case_id: Pytest id, e.g. ``"new_rotation_target_skips"``.
    :param bridge_state_session: ``session_id`` recorded in the shared
        bridge state, e.g. ``"3bb59abc6e20b834cbb2269f28880895"``, or
        ``None`` to leave the bridge unseeded (fresh session).
    :param terminal_under: Session id that owns a live ``codex:main``
        terminal in the registry, or ``None`` for no live terminal.
    :param bridge_id_label: Value reported for the session's
        :data:`CODEX_NATIVE_BRIDGE_ID_LABEL_KEY` label, e.g.
        ``"bridge_shared"`` for a rotation target (shares the original's
        bridge) or the session's own id for a fresh session.
    :param expect_auto_create: Whether the guard should invoke
        ``_auto_create_codex_terminal`` for the new session.
    """

    case_id: str
    bridge_state_session: str | None
    terminal_under: str | None
    bridge_id_label: str
    expect_auto_create: bool


class _CodexSnapshotServerClient:
    """
    Server-client stub for the codex auto-create guard route test.

    Answers the GETs the codex branch issues: the session snapshot
    (non-``None`` so ``_codex_session_needs_runner_terminal`` reports the
    session needs a terminal) and the labels lookup (returns the bridge-id
    label so the transfer-inbound check resolves the shared bridge dir).
    A real stub class — not ``MagicMock`` — so an unexpected call shape
    fails loudly instead of silently returning a mock.
    """

    def __init__(self, bridge_id_label: str) -> None:
        """
        :param bridge_id_label: Bridge id to report on the session's
            ``labels``, e.g. ``"bridge_shared"``.
        """
        self._bridge_id_label = bridge_id_label

    async def get(self, url: str, **kwargs: Any) -> Any:
        """
        Return a canned snapshot or labels payload for *url*.

        :param url: Request path, e.g. ``"/v1/sessions/<id>"`` or
            ``"/v1/sessions/<id>/labels"``.
        :returns: A response object exposing ``status_code`` and ``json()``
            matching the subset the runner reads.
        """
        del kwargs
        labels = {CODEX_NATIVE_BRIDGE_ID_LABEL_KEY: self._bridge_id_label}

        class _Response:
            """Minimal httpx-like response with the fields the runner reads."""

            def __init__(self, payload: dict[str, Any]) -> None:
                """:param payload: JSON body returned by ``json()``."""
                self.status_code = 200
                self._payload = payload

            def json(self) -> dict[str, Any]:
                """:returns: The canned JSON payload."""
                return self._payload

        if url.endswith("/labels"):
            return _Response({"labels": labels})
        return _Response({"id": "2d1b1a96e3e08f2cd43c0cc4b695ac5d", "labels": labels})


class _BlockingCodexRecoveryServerClient:
    """Block durable inbox recovery while rejecting redundant init reads."""

    def __init__(self) -> None:
        self.recovery_started = asyncio.Event()
        self.release_recovery = asyncio.Event()
        self.recovery_completed = asyncio.Event()
        self.requests: list[str] = []
        self.launch_snapshot: dict[str, Any] | None = None

    async def get(self, url: str, **kwargs: Any) -> Any:
        """Serve recovery/history; metadata reads must be explicitly enabled."""
        del kwargs
        self.requests.append(url)
        if url.endswith("/child_sessions"):
            self.recovery_started.set()
            await self.release_recovery.wait()
            self.recovery_completed.set()
            return httpx.Response(
                200,
                json={"data": [], "has_more": False},
                request=httpx.Request("GET", url),
            )
        if url.endswith("/items"):
            return httpx.Response(
                200,
                json={"data": [], "has_more": False},
                request=httpx.Request("GET", url),
            )
        if (
            self.launch_snapshot is not None
            and url == f"/v1/sessions/{self.launch_snapshot['id']}"
        ):
            return httpx.Response(
                200, json=self.launch_snapshot, request=httpx.Request("GET", url)
            )
        raise AssertionError(f"unexpected runner-init GET: {url}")


class _DeletingCodexRecoveryServerClient:
    """Pause the first recovery after one child is delivered."""

    def __init__(self, parent_id: str, first_child_id: str, second_child_id: str) -> None:
        self.parent_id = parent_id
        self.first_child_id = first_child_id
        self.second_child_id = second_child_id
        self.scan_count = 0
        self.second_child_read_started = asyncio.Event()
        self.first_recovery_cancelled = asyncio.Event()

    def _child_summary(self, child_id: str, dispatch_id: str) -> dict[str, Any]:
        return {
            "id": child_id,
            "tool": "reviewer",
            "session_name": child_id,
            "current_task_status": "completed",
            "labels": {subagent_work.SUBAGENT_DISPATCH_ID_LABEL_KEY: dispatch_id},
        }

    @staticmethod
    def _response(payload: dict[str, Any]) -> httpx.Response:
        return httpx.Response(
            200,
            json=payload,
            request=httpx.Request("GET", "http://test-server"),
        )

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        del kwargs
        if url.endswith(f"/{self.parent_id}/child_sessions"):
            self.scan_count += 1
            return self._response(
                {
                    "data": [
                        self._child_summary(self.first_child_id, "dispatch-first"),
                        self._child_summary(self.second_child_id, "dispatch-second"),
                    ],
                    "has_more": False,
                }
            )
        if url.endswith(f"/{self.first_child_id}/items"):
            return self._response(
                {
                    "data": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "first result"}],
                        }
                    ],
                    "has_more": False,
                }
            )
        if url.endswith(f"/{self.second_child_id}/items"):
            if self.scan_count == 1:
                self.second_child_read_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.first_recovery_cancelled.set()
                    raise
            return self._response(
                {
                    "data": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "second result"}],
                        }
                    ],
                    "has_more": False,
                }
            )
        if url.endswith(f"/{self.parent_id}/items"):
            return self._response({"data": [], "has_more": False})
        raise AssertionError(f"unexpected runner-init GET: {url}")

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        del kwargs
        return httpx.Response(200, request=httpx.Request("POST", url))


_CODEX_AUTO_CREATE_SCENARIOS = [
    # Rotation target: the bridge's active session still owns the live codex
    # terminal that is about to be transferred onto the new session.
    _CodexAutoCreateScenario(
        case_id="new_rotation_target_skips",
        bridge_state_session="3bb59abc6e20b834cbb2269f28880895",
        terminal_under="3bb59abc6e20b834cbb2269f28880895",
        bridge_id_label="bridge_shared",
        expect_auto_create=False,
    ),
    # Fresh host-spawned session: own bridge, no recorded state, no terminal —
    # it must bootstrap its own Codex.
    _CodexAutoCreateScenario(
        case_id="fresh_session_creates",
        bridge_state_session=None,
        terminal_under=None,
        bridge_id_label="2d1b1a96e3e08f2cd43c0cc4b695ac5d",
        expect_auto_create=True,
    ),
    # The bridge's recorded session is the new session itself (e.g. a relaunch
    # after the terminal died) — not a rotation, so auto-create proceeds.
    _CodexAutoCreateScenario(
        case_id="active_is_self_creates",
        bridge_state_session="2d1b1a96e3e08f2cd43c0cc4b695ac5d",
        terminal_under=None,
        bridge_id_label="bridge_shared",
        expect_auto_create=True,
    ),
    # The bridge names a sibling but no live terminal exists under it —
    # nothing to transfer in, so auto-create proceeds.
    _CodexAutoCreateScenario(
        case_id="dead_terminal_under_active_creates",
        bridge_state_session="3bb59abc6e20b834cbb2269f28880895",
        terminal_under=None,
        bridge_id_label="bridge_shared",
        expect_auto_create=True,
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    _CODEX_AUTO_CREATE_SCENARIOS,
    ids=[s.case_id for s in _CODEX_AUTO_CREATE_SCENARIOS],
)
async def test_create_session_codex_auto_create_guard_skips_rotation_targets(
    scenario: _CodexAutoCreateScenario,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The codex-native auto-create guard skips ``/new`` rotation targets.

    A native Codex ``/new`` starts a fresh thread in the SAME terminal, and
    the forwarder rotates Omnigent ownership onto a fresh session before
    transferring that terminal onto it. The bind reaches the runner's
    ``POST /v1/sessions`` before the transfer runs, so the new session
    momentarily has no terminal. Auto-creating a second ``codex:main`` here
    makes the rotation's transfer 409, so the terminal — and the tmux status
    link it carries — stays owned by the superseded session while the web
    session streams from the new one.

    Drives the real route with the real guard. Each scenario seeds the shared
    bridge state's ``session_id`` and the terminal registry, then asserts
    whether ``_auto_create_codex_terminal`` ran. Reverting the guard turns the
    ``new_rotation_target_skips`` case red. Mirrors the claude-native and
    antigravity-native guard tests above.
    """
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.bridge._BRIDGE_ROOT",
        tmp_path / "codex-native",
    )

    # Seed the shared bridge state so the guard reads the original
    # (terminal-owning) session as the bridge's active session.
    if scenario.bridge_state_session is not None:
        seed_dir = prepare_codex_bridge_dir(scenario.bridge_id_label)
        write_codex_bridge_state(
            seed_dir,
            CodexNativeBridgeState(
                session_id=scenario.bridge_state_session,
                socket_path="ws://127.0.0.1:9876",
                thread_id="thread_old",
                codex_home=str(tmp_path / "codex-home"),
            ),
        )

    # Seed a live codex:main terminal under the original session so the guard's
    # registry probe finds the terminal that would be transferred. Poking
    # ``_by_conversation`` directly is the established registry-test idiom — a
    # real TerminalInstance without launching tmux.
    terminal_registry = TerminalRegistry()
    if scenario.terminal_under is not None:
        instance = TerminalInstance(
            name="codex",
            session_key="main",
            socket_path=tmp_path / "codex.sock",
            private_dir=tmp_path / "codex",
            running=True,
        )
        terminal_registry._by_conversation[scenario.terminal_under] = {("codex", "main"): instance}

    created: list[str] = []

    async def _recording_auto_create(
        session_id: str, resource_registry: Any, publish_event: Any, **_kwargs: Any
    ) -> None:
        """
        Record the auto-create call instead of launching a real Codex.

        :param session_id: Session id the guard chose to auto-create for.
        :param resource_registry: Unused — the real launch path is stubbed.
        :param publish_event: Unused — the real launch path is stubbed.
        :param _kwargs: Absorbs keyword args added to the real function.
        :returns: None.
        """
        del resource_registry, publish_event
        created.append(session_id)

    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._auto_create_codex_terminal",
        _recording_auto_create,
    )

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """
        Return the codex-native spec for any agent id.

        :param agent_id: Requested agent id (unused — fixed spec).
        :param session_id: Requested session id (unused — fixed spec).
        :returns: The codex-native :class:`AgentSpec`.
        """
        del agent_id, session_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_CodexSnapshotServerClient(  # type: ignore[arg-type]
            scenario.bridge_id_label
        ),
        terminal_registry=terminal_registry,
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "2d1b1a96e3e08f2cd43c0cc4b695ac5d",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
    assert resp.status_code == 201, resp.text

    if scenario.expect_auto_create:
        # Fresh / no-live-sibling sessions must still bootstrap their own
        # Codex — the guard only suppresses true rotation targets.
        assert created == ["2d1b1a96e3e08f2cd43c0cc4b695ac5d"], (
            f"Expected auto-create for {scenario.case_id}; got {created}"
        )
    else:
        # The rotation target's terminal arrives via transfer. Auto-create here
        # is the regression: it 409s the transfer, so the terminal and its tmux
        # status link stay on the superseded session.
        assert created == [], f"Auto-create must be skipped for {scenario.case_id}; got {created}"


@pytest.mark.asyncio
async def test_create_session_codex_envelope_avoids_reads_and_overlaps_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Init avoids metadata reads; a later terminal ensure reads changed config."""
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://test-server")
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.bridge._BRIDGE_ROOT",
        tmp_path / "codex-native",
    )
    terminal_started = asyncio.Event()
    launch_configs: list[Any] = []

    async def _recording_auto_create(
        session_id: str,
        resource_registry: Any,
        publish_event: Any,
        **kwargs: Any,
    ) -> SessionResourceView:
        del resource_registry, publish_event
        launch_configs.append(
            await _codex_native_launch_config(
                session_id=session_id,
                server_client=kwargs["server_client"],
                session_init=kwargs.get("session_init"),
            )
        )
        terminal_started.set()
        return SessionResourceView(
            id="terminal_codex_main", type="terminal", session_id=session_id, name="Codex"
        )

    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._auto_create_codex_terminal",
        _recording_auto_create,
    )

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return native_spec

    session_id = "f1a92609dd7840ae9f2c4c29127c2a61"
    agent_id = "7d594e9075c74249a846df420dfa7fcb"
    server_client = _BlockingCodexRecoveryServerClient()
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
        terminal_registry=TerminalRegistry(),
    )

    async with _runner_client(app) as client:
        request_task = asyncio.create_task(
            client.post(
                "/v1/sessions",
                json={
                    "session_id": session_id,
                    "agent_id": agent_id,
                    "session_init": {
                        "protocol_version": 2,
                        "server_version": "0.13.0.dev5",
                        "session_id": session_id,
                        "agent_id": agent_id,
                        "snapshot": {
                            "created_at": 10,
                            "updated_at": 11,
                            "workspace": str(tmp_path),
                            "labels": {CODEX_NATIVE_BRIDGE_ID_LABEL_KEY: session_id},
                            "model_override": "gpt-5.4-mini",
                            "reasoning_effort": "high",
                            "external_session_id": None,
                            "terminal_launch_args": ["--config", "approval_policy=on-request"],
                            "harness_override": "codex-native",
                            "cost_control_mode_override": None,
                        },
                    },
                },
            )
        )
        recovery_wait = asyncio.create_task(server_client.recovery_started.wait())
        done, _ = await asyncio.wait(
            {request_task, recovery_wait},
            timeout=budget(1.0),
            return_when=asyncio.FIRST_COMPLETED,
        )
        assert done, "session init did not start recovery within the test budget"
        assert recovery_wait in done, (
            f"session init finished before recovery started: "
            f"{request_task.result().status_code} {request_task.result().text}"
        )
        await asyncio.wait_for(terminal_started.wait(), timeout=budget(1.0))
        assert not request_task.done()
        server_client.release_recovery.set()
        resp = await request_task

        assert resp.status_code == 201, resp.text
        assert len(launch_configs) == 1
        assert launch_configs[0].model_override == "gpt-5.4-mini"
        assert launch_configs[0].external_session_id is None
        assert server_client.requests == [
            f"/v1/sessions/{session_id}/child_sessions",
            f"/v1/sessions/{session_id}/items",
        ]

        server_client.launch_snapshot = {
            "id": session_id,
            "workspace": str(tmp_path),
            "model_override": "gpt-5.4",
            "external_session_id": "thread_current",
            "terminal_launch_args": ["--config", "approval_policy=never"],
        }
        ensured = await client.post(
            f"/v1/sessions/{session_id}/resources/terminals",
            json={"terminal": "codex", "session_key": "main", "ensure_native_terminal": True},
        )

    assert ensured.status_code == 200, ensured.text
    assert len(launch_configs) == 2
    assert launch_configs[1].model_override == "gpt-5.4"
    assert launch_configs[1].external_session_id == "thread_current"
    assert launch_configs[1].terminal_launch_args == ["--config", "approval_policy=never"]
    assert server_client.requests[-1] == f"/v1/sessions/{session_id}"


@pytest.mark.asyncio
async def test_create_session_keeps_recovery_alive_when_pre_launch_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal pre-launch failure must not orphan concurrent inbox recovery."""

    async def _raise_pre_launch_error(**_kwargs: Any) -> bool:
        raise RuntimeError("pre-launch failed")

    monkeypatch.setattr(
        "omnigent.runner.app._codex_native_terminal_arrives_via_transfer",
        _raise_pre_launch_error,
    )
    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return native_spec

    session_id = "c46cf86fc9674eac9097e3fb260ce85f"
    agent_id = "5b491851c683439981e6c2188929fcc0"
    server_client = _BlockingCodexRecoveryServerClient()
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
        terminal_registry=TerminalRegistry(),
    )

    async with _runner_client(app) as client:
        with pytest.raises(RuntimeError, match="pre-launch failed"):
            await client.post(
                "/v1/sessions",
                json={
                    "session_id": session_id,
                    "agent_id": agent_id,
                    "session_init": {
                        "protocol_version": 2,
                        "server_version": "0.13.0.dev5",
                        "session_id": session_id,
                        "agent_id": agent_id,
                        "snapshot": {
                            "created_at": 10,
                            "updated_at": 11,
                            "workspace": str(tmp_path),
                            "labels": {CODEX_NATIVE_BRIDGE_ID_LABEL_KEY: session_id},
                        },
                    },
                },
            )
        await asyncio.wait_for(server_client.recovery_started.wait(), timeout=1.0)
        server_client.release_recovery.set()
        await asyncio.wait_for(server_client.recovery_completed.wait(), timeout=1.0)


@pytest.mark.asyncio
async def test_delete_cancels_recovery_before_same_session_reinitializes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deletion must stop stale recovery before a same-id session is recreated."""
    pre_launch_calls = 0

    async def _fail_first_pre_launch(**_kwargs: Any) -> bool:
        nonlocal pre_launch_calls
        pre_launch_calls += 1
        if pre_launch_calls == 1:
            raise RuntimeError("pre-launch failed")
        return False

    async def _skip_auto_create(
        session_id: str,
        resource_registry: Any,
        publish_event: Any,
        **_kwargs: Any,
    ) -> None:
        del session_id, resource_registry, publish_event

    async def _skip_bridge_cleanup(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "omnigent.runner.app._codex_native_terminal_arrives_via_transfer",
        _fail_first_pre_launch,
    )
    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._auto_create_codex_terminal",
        _skip_auto_create,
    )
    monkeypatch.setattr(
        "omnigent.runner.app._delete_native_bridge_dirs",
        _skip_bridge_cleanup,
    )

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return native_spec

    session_id = "97a1ce7a249c4f64aacb9064f690e639"
    agent_id = "fe373ed32ac948d599d50b52680d1c62"
    first_child_id = "4f214b07944c436ea21c47eec87ecbb2"
    second_child_id = "053550e3650144248e99f77995976377"
    server_client = _DeletingCodexRecoveryServerClient(
        session_id,
        first_child_id,
        second_child_id,
    )
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
        terminal_registry=TerminalRegistry(),
    )
    body = {
        "session_id": session_id,
        "agent_id": agent_id,
        "session_init": {
            "protocol_version": 2,
            "server_version": "0.13.0.dev5",
            "session_id": session_id,
            "agent_id": agent_id,
            "snapshot": {
                "created_at": 10,
                "updated_at": 11,
                "workspace": str(tmp_path),
                "labels": {CODEX_NATIVE_BRIDGE_ID_LABEL_KEY: session_id},
            },
        },
    }

    async with _runner_client(app) as client:
        with pytest.raises(RuntimeError, match="pre-launch failed"):
            await client.post("/v1/sessions", json=body)
        await asyncio.wait_for(server_client.second_child_read_started.wait(), timeout=1.0)
        first_entry = subagent_work.get_subagent_work(first_child_id)
        assert first_entry is not None and first_entry.delivered
        assert subagent_work._session_inboxes_ref[session_id].qsize() == 1

        delete_resp = await client.delete(f"/v1/sessions/{session_id}")
        assert delete_resp.status_code == 200, delete_resp.text
        assert server_client.first_recovery_cancelled.is_set()
        assert subagent_work.get_subagent_work(first_child_id) is None
        assert session_id not in subagent_work._session_inboxes_ref

        recreate_resp = await client.post("/v1/sessions", json=body)
        assert recreate_resp.status_code == 201, recreate_resp.text
        recovered = []
        inbox = subagent_work._session_inboxes_ref[session_id]
        while not inbox.empty():
            recovered.append(inbox.get_nowait())
        assert any(
            payload.get("task_id") == first_child_id and payload.get("output") == "first result"
            for payload in recovered
        )
        assert server_client.scan_count == 2

        cleanup_resp = await client.delete(f"/v1/sessions/{session_id}")
        assert cleanup_resp.status_code == 200, cleanup_resp.text


class _RecordingCodexRegistry:
    """Captures the launched Codex terminal spec."""

    terminal_registry = None

    def __init__(self, captured: dict[str, Any]) -> None:
        self.captured = captured

    async def launch_auxiliary_terminal(
        self,
        *,
        session_id: str,
        terminal_name: str,
        session_key: str,
        spec: Any,
        resource_role: str | None = None,
        parent_os_env: Any = None,
    ) -> SessionResourceView:
        """Record the spec and return a terminal resource view."""
        del terminal_name, session_key, resource_role, parent_os_env
        self.captured["spec"] = spec
        return SessionResourceView(
            id="terminal_codex_main", type="terminal", session_id=session_id, name="codex:main"
        )


@pytest.mark.asyncio
async def test_codex_tui_launch_keeps_user_config_when_process_cwd_is_gone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Codex TUI relaunch retains user launch settings after the runner cwd is gone."""
    from omnigent.runner.native.orchestration import (
        _CodexNativeLaunchConfig,
        _launch_codex_native_tui,
    )

    workspace = tmp_path / "workspace"
    (workspace / ".omnigent").mkdir(parents=True)
    (workspace / ".omnigent" / "config.yaml").write_text(
        "harness:\n  codex-native:\n    command: workspace-codex\n    args: [--workspace-arg]\n"
    )
    config_home = tmp_path / "config-home"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(
        "harness:\n  codex-native:\n    command: user-codex\n    args: [--user-arg]\n"
    )
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config-home"))

    def missing_cwd() -> Path:
        raise FileNotFoundError("process cwd was removed")

    monkeypatch.setattr(Path, "cwd", missing_cwd)

    captured: dict[str, Any] = {}
    app_server = SimpleNamespace(
        listen_url="ws://127.0.0.1:9876",
        config_overrides=[],
        codex_cli_version=None,
        codex_path="/opt/codex/bin/codex",
        codex_home=tmp_path / "codex-home",
        env={},
    )
    await _launch_codex_native_tui(
        "conv_codex_cwd_gone",
        _RecordingCodexRegistry(captured),  # type: ignore[arg-type]
        lambda _sid, _evt: None,
        app_server=app_server,  # type: ignore[arg-type]
        launch_config=_CodexNativeLaunchConfig(
            workspace=workspace,
            policy_server_url="http://127.0.0.1:8000",
            terminal_launch_args=None,
            model_override=None,
            external_session_id=None,
            fork_source_id=None,
            fork_source_external_id=None,
            fork_carry_history=False,
            bypass_sandbox=False,
        ),
        bridge_dir=tmp_path / "bridge",
        thread_id=None,
        agent_spec=None,
    )

    spec = captured["spec"]
    assert spec.command == "user-codex"
    assert spec.args[0] == "--user-arg"
    assert spec.os_env.cwd == str(workspace)


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_registers_permission_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Host-spawned terminal launch wires the PermissionRequest hook.

    The runner's ``_auto_create_claude_terminal`` is the launch path
    used when a claude-native session is created with no CLI client
    present (web-UI sessions, the ``omnigent host`` host API). It
    must pass the Omnigent server URL into ``augment_claude_args`` so
    ``build_hook_settings`` registers the ``PermissionRequest`` command
    hook and writes permission_hook.json. Without it, approval prompts
    silently never reach the web UI even though every other hook is
    present (the regression observed in production: settings carried
    SessionStart/Stop/.../PreCompact + statusLine but no
    PermissionRequest).
    """
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")

    isolate_provider_resolution(monkeypatch, tmp_path / "config")
    token_calls: list[int] = []
    loop = asyncio.get_running_loop()
    loop_progressed: list[bool] = []

    def _shared_auth_factory() -> str:
        token_calls.append(1)
        released = threading.Event()
        loop.call_soon_threadsafe(released.set)
        loop_progressed.append(released.wait(timeout=1.0))
        return "shared-runner-token"

    def _unexpected_auth_resolution(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise AssertionError("terminal launch must reuse the runner auth factory")

    monkeypatch.setattr(
        "omnigent.runner._entry._make_auth_token_factory",
        _unexpected_auth_resolution,
    )

    # The real forwarder opens an HTTP stream to the server; stub it so
    # the auto-create flow runs without network. The created task is
    # scheduled and completes immediately. Capture the kwargs so the
    # test can assert the forwarder gets a refresh-capable auth.
    forwarder_kwargs: dict[str, Any] = {}

    async def _no_op_forwarder(**kwargs: Any) -> None:
        forwarder_kwargs.update(kwargs)

    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.forwarder.supervise_forwarder",
        _no_op_forwarder,
    )

    captured: dict[str, Any] = {}

    class _FakeResourceRegistry:
        """Captures the launched terminal spec; no live terminal registry."""

        # ``_publish_tmux_target_for_bridge`` early-returns when this is
        # None, so the test doesn't need a real terminal instance.
        terminal_registry = None

        async def launch_required_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """Record the spec and return a terminal resource view."""
            del terminal_name, session_key
            captured["spec"] = spec
            return SessionResourceView(
                id="terminal_claude_main",
                type="terminal",
                session_id=session_id,
                name="claude:main",
                metadata={"terminal_name": "claude", "session_key": "main", "running": True},
            )

    await _auto_create_claude_terminal(
        "4e92b5a0c0ee6db3f874f9c4a3f855a5",
        _FakeResourceRegistry(),
        lambda _sid, _evt: None,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        auth_token_factory=_shared_auth_factory,
    )

    spec = captured["spec"]
    assert spec.command == "claude"
    assert spec.env["ENABLE_TOOL_SEARCH"] == "true"
    # The claude-native terminal must opt into keeping its private tmux server
    # alive past an inner-CLI exit, so a sub-agent worker whose `claude` exits
    # early no longer cascades into "no server running" (#540).
    assert spec.keep_alive_after_exit is True
    args = spec.args
    assert "--append-system-prompt" not in args
    settings = _load_claude_invocation_settings(args)
    assert "PermissionRequest" in settings["hooks"]
    permission_hook = settings["hooks"]["PermissionRequest"][0]["hooks"][0]
    assert permission_hook["type"] == "command"
    assert "claude_native.hook permission-request" in permission_hook["command"]

    # The hook reads the server URL back out of this file at hook time,
    # so it must be written with the runner's Omnigent server URL.
    config = read_permission_hook_config(
        bridge_dir_for_bridge_id("4e92b5a0c0ee6db3f874f9c4a3f855a5")
    )
    assert config["ap_server_url"] == "http://127.0.0.1:8000"
    assert config["ap_auth_headers"]["Authorization"] == "Bearer shared-runner-token"
    assert token_calls == [1]
    assert loop_progressed == [True], "Claude hook token refresh blocked the runner event loop"

    # The forwarder must get a refresh-capable httpx.Auth (not just a
    # one-shot Authorization header) so a long-running host-spawned
    # session keeps forwarding after the ~1h OAuth token expires.
    # ``_auto_create_claude_terminal`` schedules the forwarder as a task;
    # yield once so the stub records its kwargs before asserting.
    from omnigent.runner._entry import _RunnerDatabricksAuth

    await asyncio.sleep(0)
    assert isinstance(forwarder_kwargs.get("auth"), _RunnerDatabricksAuth)


# ── What a plain claude-native launch must NOT carry ─────────────────
#
# A session created without Smart Routing has to launch byte-for-byte like a
# pre-Smart-Routing one: no spawn-routing endpoint (so no loopback server, no
# bearer token on disk, and no ``Task`` PreToolUse hook paying a hook
# subprocess plus a round trip on every spawn), and no custom-picker-slot pin
# displacing the workspace's own picker row.
#
# Accepted consequence: the class is stamped at create, so flipping the gear's
# Subagent-routing toggle on for a plain session is inert until recreate.


async def _run_auto_create_claude_terminal_for_routing_class(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_id: str,
    routed: bool,
    auto_harness: bool = False,
) -> Any:
    """Drive the claude-native launch and return the captured terminal spec.

    :param routed: Stamp ``cost_control_mode_override="on"``, as the create
        path does for a session launched on Smart Routing.
    :param auto_harness: Stamp the auto-harness label and sentinel WITHOUT the
        cost-control field, which is the shape a sub-agent child of a routed
        parent is created with.
    """
    from omnigent.harnesses.claude_native.main import ClaudeNativeUcodeConfig

    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    config_home = tmp_path / "config-home"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(
        "auth:\n  type: databricks\n  profile: test-profile\n"
    )
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))

    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.forwarder.supervise_forwarder",
        _no_op_forwarder,
    )

    # ``opus`` resolves to the newer generation, so the launch model
    # (opus-4-7) has no spelling of its own — exactly the case the pin was
    # added for, and the case that overwrites the workspace's picker row.
    ucode = ClaudeNativeUcodeConfig(
        env={
            "ANTHROPIC_BASE_URL": "https://gw.example/anthropic",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "databricks-claude-opus-5",
            "ANTHROPIC_CUSTOM_MODEL_OPTION": "workspace-picker-row",
            "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": "Workspace pick",
        },
        api_key_helper="printf %s sk-sentinel-do-not-use",
        model="databricks-claude-opus-4-7",
    )
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.main._ucode_config_for_profile",
        lambda profile, *, refresh_models=True: ucode,
    )

    captured: dict[str, Any] = {}

    class _FakeResourceRegistry:
        terminal_registry = None

        async def launch_required_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            del terminal_name, session_key
            captured["spec"] = spec
            return SessionResourceView(
                id="terminal_claude_main",
                type="terminal",
                session_id=session_id,
                name="claude:main",
                metadata={"terminal_name": "claude", "session_key": "main", "running": True},
            )

    snapshot: dict[str, Any] = {"labels": {}}
    if routed:
        snapshot["cost_control_mode_override"] = "on"
    if auto_harness:
        from omnigent.runner.subagent_routing import AUTO_HARNESS_LABEL_KEY

        snapshot["labels"][AUTO_HARNESS_LABEL_KEY] = "1"
        snapshot["harness_override"] = "auto"
    fake_client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=snapshot)),
    )
    try:
        await _auto_create_claude_terminal(
            session_id,
            _FakeResourceRegistry(),
            lambda _sid, _evt: None,
            server_client=fake_client,
        )
    finally:
        from omnigent.runner import subagent_routing, turn_routing

        subagent_routing.shutdown_session_router(session_id)
        turn_routing.shutdown_session_turn_router(session_id)
        await fake_client.aclose()
    return captured["spec"]


def _claude_pretooluse_matchers(spec: Any) -> list[str | None]:
    settings = _load_claude_invocation_settings(spec.args)
    return [entry.get("matcher") for entry in settings["hooks"].get("PreToolUse", [])]


def _claude_hook_commands(spec: Any) -> list[str]:
    settings = _load_claude_invocation_settings(spec.args)
    return [
        hook.get("command", "")
        for entries in settings["hooks"].values()
        for entry in entries
        for hook in entry.get("hooks", [])
    ]


async def test_auto_create_claude_terminal_loads_workspace_agents_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    skill = workspace / ".agents" / "skills" / "portable" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: portable\ndescription: Portable skill\n---\nUse this skill.\n")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(workspace))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    spec = await _run_auto_create_claude_terminal_for_routing_class(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        session_id="4a1c9b1d1f0e4c5da0a1b2c3d4e5f604",
        routed=False,
    )

    overlay = Path(spec.args[spec.args.index("--add-dir") + 1])
    exposed = list((overlay / ".claude" / "skills").glob("*/SKILL.md"))
    assert [path.parent.name for path in exposed] == ["portable"]
    assert [path.read_text() for path in exposed] == [skill.read_text()]


async def test_a_plain_claude_native_launch_carries_no_spawn_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = await _run_auto_create_claude_terminal_for_routing_class(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        session_id="4a1c9b1d1f0e4c5da0a1b2c3d4e5f601",
        routed=False,
    )

    assert claude_native_bridge.CLAUDE_SUBAGENT_TOOL_MATCHER not in _claude_pretooluse_matchers(
        spec
    )
    assert all("claude_router_hook" not in command for command in _claude_hook_commands(spec))
    # The workspace's own picker row survives untouched.
    assert spec.env["ANTHROPIC_CUSTOM_MODEL_OPTION"] == "workspace-picker-row"


async def test_a_routed_claude_native_launch_keeps_the_spawn_gate_and_the_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = await _run_auto_create_claude_terminal_for_routing_class(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        session_id="4a1c9b1d1f0e4c5da0a1b2c3d4e5f602",
        routed=True,
    )

    assert claude_native_bridge.CLAUDE_SUBAGENT_TOOL_MATCHER in _claude_pretooluse_matchers(spec)
    assert any("claude_router_hook" in command for command in _claude_hook_commands(spec))
    assert spec.env["ANTHROPIC_CUSTOM_MODEL_OPTION"] == "databricks-claude-opus-4-7"
    # A pinned session's spawns stay on the claude family, so it gets neither
    # the routed-spawn note nor the pre-approvals the cross-family hop needs.
    assert "--append-system-prompt" not in spec.args


async def test_an_auto_harness_launch_without_a_cost_control_stamp_is_still_routed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sub-agent child of a routed parent: auto-harness label, no cost stamp.

    Such a child is created with ``harness_override="auto"`` and the
    auto-harness label but no ``cost_control_mode_override``. Deriving
    ``routing_enabled`` from the cost field alone launched it with the
    routed-spawn note and the four pre-approved tools while starting no router
    and pinning no arms — an argv that tells Claude to route spawns through a
    hook nothing answers.
    """
    spec = await _run_auto_create_claude_terminal_for_routing_class(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        session_id="4a1c9b1d1f0e4c5da0a1b2c3d4e5f603",
        routed=False,
        auto_harness=True,
    )

    # The whole routed apparatus, not just the spawn note.
    assert claude_native_bridge.CLAUDE_SUBAGENT_TOOL_MATCHER in _claude_pretooluse_matchers(spec)
    assert any("claude_router_hook" in command for command in _claude_hook_commands(spec))
    assert spec.env["ANTHROPIC_CUSTOM_MODEL_OPTION"] == "databricks-claude-opus-4-7"
    # And the auto-harness extras, which only make sense alongside the router.
    assert "--append-system-prompt" in spec.args
    allowed = spec.args[spec.args.index("--allowedTools") + 1]
    assert "mcp__omnigent__sys_session_create" in allowed


def test_routed_spawn_launch_args_need_a_router() -> None:
    """The note and pre-approvals never ship without the router that serves them."""
    from omnigent.runner.native.orchestration import _routed_spawn_launch_args

    note, tools = _routed_spawn_launch_args(True)
    assert note and tools
    assert _routed_spawn_launch_args(True, router_started=False) == (None, ())
    assert _routed_spawn_launch_args(False) == (None, ())


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["subscription", "gateway", "bound"])
async def test_auto_create_claude_terminal_launch_gate_folds_a_canonical_override(
    endpoint: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A persisted canonical id the catalog lists only by family still launches.

    A live ``/model`` persists the pane's exact id (``claude-opus-4-8``) while
    the catalog spells that family as alias rows and the 1M default. On a
    canonical endpoint the relaunch must pass the id through as ``--model``
    rather than refuse the resume; a gateway, which routes only its own
    spellings, launches on its own default instead and resets the pick to Default.
    """
    from omnigent.harnesses.claude_native.main import ClaudeNativeUcodeConfig
    from omnigent.inference_config import inference_config_scope

    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")

    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.forwarder.supervise_forwarder",
        _no_op_forwarder,
    )
    prefix = "" if endpoint == "subscription" else "system.ai."
    catalog = [
        {"id": "opus", "model": f"{prefix}claude-opus-5", "displayName": "Opus 5"},
        {
            "id": f"{prefix}claude-opus-4-8[1m]",
            "model": f"{prefix}claude-opus-4-8[1m]",
            "displayName": "Opus 4.8 (1M context)",
            "isDefault": True,
        },
    ]

    async def _catalog(config: object) -> list[dict[str, object]]:
        del config
        return catalog

    monkeypatch.setattr("omnigent.harnesses.claude_native.main.claude_launch_catalog", _catalog)

    captured: dict[str, Any] = {}

    patches: list[dict[str, Any]] = []
    selected_model = "private/model-b[large]" if endpoint == "bound" else "claude-opus-4-8"

    def _handle_request(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH":
            patches.append(json.loads(request.content))
        return httpx.Response(200, json={"model_override": selected_model, "labels": {}})

    fake_client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(_handle_request),
    )
    config = (
        None
        if endpoint == "subscription"
        else ClaudeNativeUcodeConfig(
            env={"ANTHROPIC_BASE_URL": "https://gateway.example/anthropic"},
            api_key_helper="printf %s sk-gateway",
            model="system.ai.claude-opus-5",
        )
    )

    async def _resolve() -> ClaudeNativeUcodeConfig | None:
        return config

    session_id = "0f2d3d5c9a6b4e1f8c7d6e5f4a3b2c1d"
    inference = (
        {
            "providers": {"gateway": {"kind": "gateway"}},
            "inference": {
                "harnesses": {
                    "claude-native": {
                        "provider": "gateway",
                        "default_model": "private/model-a",
                        "model_allowlist": ["private/model-a", selected_model],
                    }
                }
            },
        }
        if endpoint == "bound"
        else {}
    )
    with inference_config_scope(inference):
        await _auto_create_claude_terminal(
            session_id,
            _RecordingClaudeRegistry(captured),
            lambda _sid, _evt: None,
            server_client=fake_client,
            resolve_launch_config=_resolve,
        )
    args = captured["spec"].args
    pick_resets = [body for body in patches if "model_override" in body]
    if endpoint in ("subscription", "bound"):
        assert args[args.index("--model") + 1] == selected_model
        assert pick_resets == []
    else:
        assert args[args.index("--model") + 1] == "system.ai.claude-opus-5"
        assert pick_resets == [{"model_override": "default"}]

    await fake_client.aclose()


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_unserved_pick_resets_to_the_catalog_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A pick no row can serve launches on the catalog's default and resets the pick.

    The pick outlives the provider it was made under, so a relaunch that
    cannot honor it must still bring the session back: it takes the Default
    launch (the fresh catalog's ``isDefault`` row) and clears the persisted
    pick, so the picker shows the Default the session now runs.
    """
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")

    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.forwarder.supervise_forwarder",
        _no_op_forwarder,
    )
    catalog = [
        {"id": "opus", "model": "claude-opus-5", "displayName": "Opus 5"},
        {
            "id": "claude-opus-4-8[1m]",
            "model": "claude-opus-4-8[1m]",
            "displayName": "Opus 4.8 (1M context)",
            "isDefault": True,
        },
    ]

    async def _catalog(config: object) -> list[dict[str, object]]:
        del config
        return catalog

    monkeypatch.setattr("omnigent.harnesses.claude_native.main.claude_launch_catalog", _catalog)
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.main.claude_launch_catalog_is_stale",
        lambda config: False,
    )

    captured: dict[str, Any] = {}

    patches: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH":
            patches.append(json.loads(request.content))
        return httpx.Response(200, json={"model_override": "gpt-5.4", "labels": {}})

    fake_client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(_handle_request),
    )

    async def _resolve() -> None:
        return None

    await _auto_create_claude_terminal(
        "1a2b3c4d5e6f47a8b9c0d1e2f3a4b5c6",
        _RecordingClaudeRegistry(captured),
        lambda _sid, _evt: None,
        server_client=fake_client,
        resolve_launch_config=_resolve,
    )
    args = captured["spec"].args
    assert args[args.index("--model") + 1] == "claude-opus-4-8[1m]"
    assert [body for body in patches if "model_override" in body] == [
        {"model_override": "default"}
    ]

    await fake_client.aclose()


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_keeps_the_pick_when_the_terminal_fails_to_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A pick is reset only once the fallback terminal is up.

    The reset would otherwise outlive a launch that failed after the gate, so
    a tmux or bridge failure must leave the persisted pick untouched.
    """
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    catalog = [
        {"id": "opus", "model": "claude-opus-5", "displayName": "Opus 5", "isDefault": True}
    ]

    async def _catalog(config: object) -> list[dict[str, object]]:
        del config
        return catalog

    monkeypatch.setattr("omnigent.harnesses.claude_native.main.claude_launch_catalog", _catalog)
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.main.claude_launch_catalog_is_stale",
        lambda config: False,
    )

    class _FailingResourceRegistry:
        """Fails the tmux launch after the gate has run."""

        terminal_registry = None

        async def launch_required_terminal(self, **kwargs: Any) -> SessionResourceView:
            """Raise the way a tmux launch failure does."""
            del kwargs
            raise RuntimeError("tmux exited")

    patches: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH":
            patches.append(json.loads(request.content))
        return httpx.Response(200, json={"model_override": "gpt-5.4", "labels": {}})

    fake_client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(_handle_request),
    )

    async def _resolve() -> None:
        return None

    with pytest.raises(RuntimeError, match="tmux exited"):
        await _auto_create_claude_terminal(
            "3c4d5e6f7a8b49c0d1e2f3a4b5c6d7e8",
            _FailingResourceRegistry(),
            lambda _sid, _evt: None,
            server_client=fake_client,
            resolve_launch_config=_resolve,
        )
    assert [body for body in patches if "model_override" in body] == []

    await fake_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh", ["serves_pick", "lacks_pick", "fails"])
async def test_auto_create_claude_terminal_refreshes_a_stale_catalog_before_resetting_the_pick(
    refresh: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A pick the stale catalog does not serve is re-checked against a fresh probe.

    The stored rows may predate a provider change, so resetting the pick on
    them alone could discard a model the provider serves today. The launch
    awaits the re-probe: a fresh row serving the pick keeps it, only a fresh
    catalog that still lacks it resets the pick, and a failed probe launches
    on the default while keeping the pick for the next relaunch.
    """
    import os
    import time

    from omnigent.harnesses.claude_native.main import claude_catalog_fingerprint
    from omnigent.models import model_catalog_store
    from tests.runner.conftest import (
        REAL_CLAUDE_LAUNCH_CATALOG,
        REAL_CLAUDE_REPROBED_LAUNCH_CATALOG,
    )

    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")

    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.forwarder.supervise_forwarder",
        _no_op_forwarder,
    )
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.main.claude_launch_catalog", REAL_CLAUDE_LAUNCH_CATALOG
    )
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.main.claude_reprobed_launch_catalog",
        REAL_CLAUDE_REPROBED_LAUNCH_CATALOG,
    )
    stale_rows = [
        {"id": "opus", "model": "claude-opus-5", "displayName": "Opus 5"},
        {
            "id": "claude-opus-4-8[1m]",
            "model": "claude-opus-4-8[1m]",
            "displayName": "Opus 4.8 (1M context)",
            "isDefault": True,
        },
    ]
    refreshed_rows = list(stale_rows)
    if refresh == "serves_pick":
        refreshed_rows.append({"id": "haiku", "model": "claude-haiku-4-5-20251001"})
    probes: list[int] = []

    async def _probe(config: object) -> list[dict[str, object]]:
        del config
        probes.append(1)
        if refresh == "fails":
            raise OSError("provider unreachable")
        return refreshed_rows

    monkeypatch.setattr("omnigent.harnesses.claude_native.main.claude_model_catalog", _probe)
    fingerprint = claude_catalog_fingerprint(None)
    model_catalog_store.write_catalog("claude-native", fingerprint, stale_rows)
    path = model_catalog_store.catalog_path("claude-native", fingerprint)
    old = time.time() - (model_catalog_store.CATALOG_STALE_AFTER_S + 60)
    os.utime(path, (old, old))

    captured: dict[str, Any] = {}

    patches: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH":
            patches.append(json.loads(request.content))
        return httpx.Response(200, json={"model_override": "claude-haiku-4-5", "labels": {}})

    fake_client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(_handle_request),
    )

    async def _resolve() -> None:
        return None

    await _auto_create_claude_terminal(
        "2b3c4d5e6f7a48b9c0d1e2f3a4b5c6d7",
        _RecordingClaudeRegistry(captured),
        lambda _sid, _evt: None,
        server_client=fake_client,
        resolve_launch_config=_resolve,
    )
    args = captured["spec"].args
    assert probes == [1], "the launch must await exactly one re-probe"
    pick_resets = [body for body in patches if "model_override" in body]
    if refresh == "serves_pick":
        assert model_catalog_store.read_catalog("claude-native", fingerprint) == refreshed_rows
        assert args[args.index("--model") + 1] == "claude-haiku-4-5"
        assert pick_resets == []
    elif refresh == "lacks_pick":
        assert model_catalog_store.read_catalog("claude-native", fingerprint) == refreshed_rows
        assert args[args.index("--model") + 1] == "claude-opus-4-8[1m]"
        assert pick_resets == [{"model_override": "default"}]
    else:
        # No fresh rows: the stale entry stays, the Default launch of a stale
        # catalog goes bare, and the pick survives for the next relaunch.
        assert model_catalog_store.read_catalog("claude-native", fingerprint) == stale_rows
        assert "--model" not in args
        assert pick_resets == []

    await fake_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pin", "custom_option", "expected_launch"),
    [
        ("system.ai.claude-opus-4-8[1m]", None, "claude-opus-4-8[1m]"),
        ("sonnet_5", "system.ai.claude-sonnet-5", "claude-sonnet-5"),
    ],
    ids=["gateway-namespace-pin", "custom-slot-pin"],
)
async def test_auto_create_claude_terminal_launch_gate_folds_a_gateway_namespace_pin(
    pin: str,
    custom_option: str | None,
    expected_launch: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A pin spelling a served model in the gateway namespace still launches.

    A deployed agent-spec pin or an orchestrator pick persists the catalog
    spelling (``system.ai.claude-opus-4-8[1m]``) while the launch catalog
    lists the same model bare (``claude-opus-4-8[1m]``). The gate must fold
    the mechanical prefix away and launch on the catalog's own spelling
    rather than refuse — the refusal bricks every session of that agent.
    The custom picker slot (``sonnet_5``) only reaches the gateway spelling
    through its resolved provider option, so the fold must consult the
    resolved spelling too, and the pick must survive the launch un-reset.
    """
    from omnigent.harnesses.claude_native.main import ClaudeNativeUcodeConfig

    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")

    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.forwarder.supervise_forwarder",
        _no_op_forwarder,
    )
    catalog = [
        {"id": "opus[1m]", "model": "claude-opus-4-8[1m]", "displayName": "Opus 4.8 (1M)"},
        {"id": "sonnet", "model": "claude-sonnet-5", "displayName": "Sonnet 5", "isDefault": True},
    ]

    async def _catalog(config: object) -> list[dict[str, object]]:
        del config
        return catalog

    monkeypatch.setattr("omnigent.harnesses.claude_native.main.claude_launch_catalog", _catalog)

    captured: dict[str, Any] = {}

    patches: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH":
            patches.append(json.loads(request.content))
        return httpx.Response(200, json={"model_override": pin, "labels": {}})

    fake_client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(_handle_request),
    )
    env = {"ANTHROPIC_BASE_URL": "https://gateway.example/anthropic"}
    if custom_option is not None:
        env["ANTHROPIC_CUSTOM_MODEL_OPTION"] = custom_option
    config = ClaudeNativeUcodeConfig(
        env=env,
        api_key_helper="printf %s sk-gateway",
        model="claude-sonnet-5",
    )

    async def _resolve() -> ClaudeNativeUcodeConfig | None:
        return config

    await _auto_create_claude_terminal(
        "1a2b3c4d5e6f47899a0b1c2d3e4f5061",
        _RecordingClaudeRegistry(captured),
        lambda _sid, _evt: None,
        server_client=fake_client,
        resolve_launch_config=_resolve,
    )
    args = captured["spec"].args
    assert args[args.index("--model") + 1] == expected_launch, (
        "the gate must launch the catalog's own spelling of the pinned model"
    )
    assert [body for body in patches if "model_override" in body] == [], (
        "a foldable pin must survive the launch un-reset"
    )
    await fake_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pick", "expected_model", "reset_pick"),
    [
        ("claude-opus-4-8", "system.ai.claude-opus-5-5[1m]", True),
        ("claude-sonnet-5-5[1m]", "system.ai.claude-sonnet-5-5[1m]", False),
        ("system.ai.claude-sonnet-5-5[1m]", "system.ai.claude-sonnet-5-5[1m]", False),
    ],
)
async def test_auto_create_claude_terminal_validates_managed_gateway_pick(
    pick: str,
    expected_model: str,
    reset_pick: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.forwarder.supervise_forwarder", _no_op_forwarder
    )
    managed_settings = tmp_path / "managed-settings.json"
    managed_settings.write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://gateway.example/anthropic"}})
    )
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.main._CLAUDE_CODE_MANAGED_SETTINGS_PATHS",
        (managed_settings,),
    )
    catalog = [
        {"id": "opus", "model": "system.ai.claude-opus-5-5[1m]", "isDefault": True},
        {"id": "sonnet", "model": "system.ai.claude-sonnet-5-5[1m]"},
    ]

    async def _catalog(config: object) -> list[dict[str, object]]:
        assert config is None
        return catalog

    monkeypatch.setattr("omnigent.harnesses.claude_native.main.claude_launch_catalog", _catalog)
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.main.claude_launch_catalog_is_stale",
        lambda config: False,
    )
    captured: dict[str, Any] = {}
    patches: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH":
            patches.append(json.loads(request.content))
        return httpx.Response(200, json={"model_override": pick, "labels": {}})

    fake_client = httpx.AsyncClient(
        base_url="http://test-server", transport=httpx.MockTransport(_handle_request)
    )

    async def _resolve() -> None:
        return None

    await _auto_create_claude_terminal(
        "1a2b3c4d5e6f47899a0b1c2d3e4f5061",
        _RecordingClaudeRegistry(captured),
        lambda _sid, _evt: None,
        server_client=fake_client,
        resolve_launch_config=_resolve,
    )
    args = captured["spec"].args
    assert args[args.index("--model") + 1] == expected_model
    assert [body for body in patches if "model_override" in body] == (
        [{"model_override": "default"}] if reset_pick else []
    )
    await fake_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("freshness", ["fresh", "stale"])
async def test_auto_create_claude_terminal_default_pin_requires_a_fresh_catalog(
    freshness: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A Default launch pins the stored default only while the entry is fresh.

    The store never forgets an entry, so its ``isDefault`` row can outlive
    the model it names (a retirement or an entitlement change); pinning it
    as ``--model`` then hard-fails every Default launch on the host. A
    stale entry defers to the CLI's own default — no ``--model`` at all —
    while the store re-probes in the background.
    """
    import os
    import time

    from omnigent.harnesses.claude_native.main import claude_catalog_fingerprint
    from omnigent.models import model_catalog_store
    from tests.runner.conftest import REAL_CLAUDE_LAUNCH_CATALOG

    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")

    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.forwarder.supervise_forwarder",
        _no_op_forwarder,
    )
    # The real store-backed resolver, against the conftest-isolated store
    # dir; the background re-probe is stubbed so no real CLI ever runs.
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.main.claude_launch_catalog", REAL_CLAUDE_LAUNCH_CATALOG
    )
    refreshed = [{"id": "sonnet", "model": "claude-sonnet-5", "isDefault": True}]

    async def _fake_probe_catalog(config: object) -> list[dict[str, object]]:
        del config
        return refreshed

    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.main.claude_model_catalog", _fake_probe_catalog
    )
    fingerprint = claude_catalog_fingerprint(None)
    model_catalog_store.write_catalog(
        "claude-native",
        fingerprint,
        [
            {"id": "opus", "model": "claude-opus-5", "displayName": "Opus 5"},
            {
                "id": "claude-3-5-sonnet-20241022",
                "model": "claude-3-5-sonnet-20241022",
                "displayName": "Claude 3.5 Sonnet",
                "isDefault": True,
            },
        ],
    )
    if freshness == "stale":
        path = model_catalog_store.catalog_path("claude-native", fingerprint)
        old = time.time() - (model_catalog_store.CATALOG_STALE_AFTER_S + 60)
        os.utime(path, (old, old))

    captured: dict[str, Any] = {}

    def _handle_request(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"labels": {}})

    fake_client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(_handle_request),
    )

    async def _resolve() -> None:
        return None

    await _auto_create_claude_terminal(
        "9b1d2c3e4f5a6b7c8d9e0f1a2b3c4d5e",
        _RecordingClaudeRegistry(captured),
        lambda _sid, _evt: None,
        server_client=fake_client,
        resolve_launch_config=_resolve,
    )
    args = captured["spec"].args
    if freshness == "fresh":
        assert args[args.index("--model") + 1] == "claude-3-5-sonnet-20241022"
    else:
        assert "--model" not in args, f"a stale default was still pinned: {args}"
        task = model_catalog_store._inflight.get(("claude-native", fingerprint))
        if task is not None:
            await task
        # The background re-probe healed the store for the next launch.
        assert model_catalog_store.read_catalog("claude-native", fingerprint) == refreshed

    await fake_client.aclose()
